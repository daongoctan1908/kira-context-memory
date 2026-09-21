import { ThumbsDown, ThumbsUp } from "lucide-react";
import { useState } from "react";

import {
  clearMessageFeedback,
  setMessageFeedback,
  type MessageFeedbackRating,
} from "../conversations/conversationApi";
import { CopyMessageButton } from "./CopyMessageButton";

export function MessageActions({
  sessionId,
  turnId,
  content,
  initialRating = null,
}: {
  sessionId: string;
  turnId: string;
  content: string;
  initialRating?: MessageFeedbackRating | null;
}) {
  const [rating, setRating] = useState<MessageFeedbackRating | null>(initialRating);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);

  const toggle = async (next: MessageFeedbackRating) => {
    if (busy) return;
    const previous = rating;
    const target = rating === next ? null : next;
    setRating(target);
    setBusy(true);
    setFailed(false);
    try {
      if (target === null) {
        await clearMessageFeedback(sessionId, turnId);
      } else {
        await setMessageFeedback(sessionId, turnId, target);
      }
    } catch {
      setRating(previous);
      setFailed(true);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="message-actions" aria-label="Thao tác với câu trả lời">
      <CopyMessageButton content={content} />
      <button
        className={rating === "up" ? "message-action selected" : "message-action"}
        type="button"
        disabled={busy}
        aria-label="Câu trả lời hữu ích"
        aria-pressed={rating === "up"}
        title="Câu trả lời hữu ích"
        onClick={() => void toggle("up")}
      >
        <ThumbsUp size={17} />
      </button>
      <button
        className={rating === "down" ? "message-action selected" : "message-action"}
        type="button"
        disabled={busy}
        aria-label="Câu trả lời chưa tốt"
        aria-pressed={rating === "down"}
        title="Câu trả lời chưa tốt"
        onClick={() => void toggle("down")}
      >
        <ThumbsDown size={17} />
      </button>
      {failed ? <span className="message-feedback-error" role="status">Chưa lưu được đánh giá.</span> : null}
    </div>
  );
}
