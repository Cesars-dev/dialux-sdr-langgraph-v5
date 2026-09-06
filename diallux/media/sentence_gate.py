"""Sentence gate: LLM token stream -> TTS-ready text chunks, strictly ordered.

Custom buffering mode (Cartesia docs recommendation):
  - accumulate streamed tokens; flush on sentence-ending punctuation once a
    minimum length is reached (first sentence leaves ASAP);
  - hard cut at ~140 chars so a rambling turn still speaks early;
  - idle timeout flush (150ms without a token) so half-sentences still voice;
  - every flush uses continue=true EXCEPT the end-of-turn marker (continue=false
    minimizes latency, per Cartesia context docs; an empty final chunk is the
    documented way to close a context when the text is already flushed).

Ordering guarantee: all on_sentence calls go through one internal asyncio.Queue
with a single sender task — chunks can never reorder, even when end_of_turn's
final marker races a just-flushed sentence (the naive fire-and-forget version
could emit the closing empty chunk BEFORE the sentence, closing the Cartesia
context prematurely — caught by the test suite).

Managed mode: tokens are forwarded immediately and Cartesia's own buffering
(max_buffer_delay_ms) decides when to start speaking.
"""
from __future__ import annotations

import asyncio
import re
from typing import Awaitable, Callable

OnSentence = Callable[[str, bool], Awaitable[None]]   # (text, continue_)

_SENTENCE_END = re.compile(r"[.!?](\s|$)")
_FLUSH_LEN = 140
_MIN_LEN = 25
_IDLE_FLUSH_MS = 150


class SentenceGate:
    def __init__(self, on_sentence: OnSentence, managed: bool = False):
        self.on_sentence = on_sentence
        self.managed = managed
        self._buffer = ""
        self._timer_task: asyncio.Task | None = None
        self._closed = False
        self._last_continue_sent = False
        self._queue: asyncio.Queue[tuple[str, bool] | None] = asyncio.Queue()
        self._sender_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ #
    def add(self, token: str) -> None:
        """Called for every LLM content delta (sync — cheap, no awaits)."""
        if self._closed or not token:
            return
        self._ensure_sender()
        self._buffer += token
        if not self.managed:
            if len(self._buffer) >= _MIN_LEN:
                m = _SENTENCE_END.search(self._buffer)
                if m:
                    self._flush_upto(m.end())
            if len(self._buffer) >= _FLUSH_LEN:
                self._flush_upto(len(self._buffer))
        self._schedule_idle_flush()

    async def end_of_turn(self) -> None:
        """Turn finished: mark the context done; drains all queued chunks."""
        self._cancel_timer()
        if self._closed:
            return
        text = self._buffer.strip()
        self._buffer = ""
        if text:
            self._put(text, False)
        elif self._last_continue_sent:
            # closing marker: empty transcript + continue=false (Cartesia docs)
            self._put("", False)
        self._last_continue_sent = False
        await self._drain()

    async def reset(self) -> None:
        """Barge-in: drop buffered text and any queued-but-unsent chunks."""
        self._cancel_timer()
        self._buffer = ""
        self._last_continue_sent = False
        self._drop_queue()

    async def close(self) -> None:
        self._closed = True
        self._cancel_timer()
        self._drop_queue()

    # ------------------------------------------------------------------ #
    def _ensure_sender(self):
        if self._sender_task is None or self._sender_task.done():
            self._sender_task = asyncio.create_task(self._sender())

    def _put(self, text: str, continue_: bool):
        self._queue.put_nowait((text, continue_))

    async def _drain(self):
        """Wait until every queued chunk (incl. the final marker) was sent."""
        if self._sender_task is None:
            return
        await self._queue.join()

    async def _sender(self):
        try:
            while True:
                item = await self._queue.get()
                try:
                    if item is None:
                        return
                    text, continue_ = item
                    await self.on_sentence(text, continue_)
                finally:
                    self._queue.task_done()
        except asyncio.CancelledError:
            raise

    def _drop_queue(self):
        if self._sender_task:
            self._sender_task.cancel()
            self._sender_task = None
        # fresh queue: anything not yet sent is discarded
        self._queue = asyncio.Queue()
        self._last_continue_sent = False

    def _flush_upto(self, idx: int) -> None:
        text = self._buffer[:idx].strip()
        self._buffer = self._buffer[idx:]
        if text:
            self._last_continue_sent = True
            self._put(text, True)

    def _schedule_idle_flush(self) -> None:
        self._cancel_timer()
        if self._closed:
            return

        async def _idle():
            await asyncio.sleep(_IDLE_FLUSH_MS / 1000)
            if self._buffer.strip():
                self._flush_upto(len(self._buffer))

        self._timer_task = asyncio.create_task(_idle())

    def _cancel_timer(self) -> None:
        if self._timer_task:
            self._timer_task.cancel()
            self._timer_task = None
