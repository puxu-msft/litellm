from __future__ import annotations

import logging
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class HandlerSnapshot:
    logger: logging.Logger
    handlers: tuple[logging.Handler, ...]
    propagate: bool


def install_handler(logger: logging.Logger, handler: logging.Handler) -> HandlerSnapshot:
    snapshot = HandlerSnapshot(logger, tuple(logger.handlers), logger.propagate)
    logger.handlers = [handler]
    logger.propagate = False
    return snapshot


def restore_handler(snapshot: HandlerSnapshot) -> None:
    snapshot.logger.handlers = list(snapshot.handlers)
    snapshot.logger.propagate = snapshot.propagate
