import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "../api/http";
import {
  streamConversationMessage,
  type MessageCompletedEvent,
  type ProductChatEvent,
} from "./chatApi";

export type LiveTurnStatus =
  | "sending"
  | "streaming"
  | "completed"
  | "failed"
  | "unsaved"
  | "stopped";

export interface LiveTurn {
  clientMessageId: string;
  turnId: string | null;
  userText: string;
  assistantText: string;
  status: LiveTurnStatus;
  error: unknown;
  retryable: boolean;
  replayed: boolean;
  eventId: string | null;
}

export function useChatStream({
  sessionId,
  onCompleted,
}: {
  sessionId: string;
  onCompleted?: (event: MessageCompletedEvent) => void;
}) {
  const [turns, setTurns] = useState<LiveTurn[]>([]);
  const [activeClientMessageId, setActiveClientMessageId] = useState<string | null>(null);
  const activeController = useRef<AbortController | null>(null);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      activeController.current?.abort();
    };
  }, []);

  const updateTurn = useCallback(
    (clientMessageId: string, update: (turn: LiveTurn) => LiveTurn) => {
      if (!mounted.current) {
        return;
      }
      setTurns((current) =>
        current.map((turn) =>
          turn.clientMessageId === clientMessageId ? update(turn) : turn,
        ),
      );
    },
    [],
  );

  const runTurn = useCallback(async (clientMessageId: string, userText: string) => {
    if (activeController.current !== null) {
      return;
    }
    const controller = new AbortController();
    activeController.current = controller;
    setActiveClientMessageId(clientMessageId);
    let assistantText = "";
    const completion = { received: false };

    updateTurn(clientMessageId, (turn) => ({
      ...turn,
      assistantText: "",
      status: "sending",
      error: null,
      retryable: false,
      replayed: false,
      eventId: null,
    }));

    try {
      const completed = await streamConversationMessage({
        sessionId,
        clientMessageId,
        message: userText,
        signal: controller.signal,
        onEvent: (event: ProductChatEvent) => {
          if (event.type === "message.started") {
            updateTurn(clientMessageId, (turn) => ({
              ...turn,
              turnId: event.turn_id,
              status: "streaming",
            }));
          } else if (event.type === "message.delta") {
            assistantText += event.text;
            updateTurn(clientMessageId, (turn) => ({
              ...turn,
              assistantText,
              status: "streaming",
            }));
          } else if (event.type === "message.completed") {
            completion.received = true;
            updateTurn(clientMessageId, (turn) => ({
              ...turn,
              status: "completed",
              error: null,
              retryable: false,
              replayed: event.replayed,
              eventId: event.event_id ?? null,
            }));
          }
        },
      });
      onCompleted?.(completed);
    } catch (error) {
      if (!completion.received) {
        const stopped = controller.signal.aborted;
        const retryable = stopped || !(error instanceof ApiError) || error.retryable;
        updateTurn(clientMessageId, (turn) => ({
          ...turn,
          status: assistantText.length > 0 ? "unsaved" : stopped ? "stopped" : "failed",
          error: stopped ? null : error,
          retryable,
        }));
      }
    } finally {
      if (activeController.current === controller) {
        activeController.current = null;
        if (mounted.current) {
          setActiveClientMessageId(null);
        }
      }
    }
  }, [onCompleted, sessionId, updateTurn]);

  const send = useCallback((text: string) => {
    const userText = text.trim();
    if (userText.length === 0 || activeController.current !== null) {
      return;
    }
    const clientMessageId = crypto.randomUUID();
    setTurns((current) => [
      ...current,
      {
        clientMessageId,
        turnId: null,
        userText,
        assistantText: "",
        status: "sending",
        error: null,
        retryable: false,
        replayed: false,
        eventId: null,
      },
    ]);
    void runTurn(clientMessageId, userText);
  }, [runTurn]);

  const retry = useCallback((clientMessageId: string) => {
    if (activeController.current !== null) {
      return;
    }
    const turn = turns.find((item) => item.clientMessageId === clientMessageId);
    if (!turn?.retryable) {
      return;
    }
    void runTurn(turn.clientMessageId, turn.userText);
  }, [runTurn, turns]);

  const stop = useCallback(() => {
    activeController.current?.abort();
  }, []);

  return {
    turns,
    activeClientMessageId,
    isStreaming: activeClientMessageId !== null,
    send,
    retry,
    stop,
  };
}
