import { useLayoutEffect, useRef, useState, type KeyboardEvent } from "react";

export function ChatComposer({
  sessionId,
  streaming,
  onSend,
  onStop,
}: {
  sessionId: string;
  streaming: boolean;
  onSend: (message: string) => void;
  onStop: () => void;
}) {
  const draftKey = `kira-chat-draft:${sessionId}`;
  const [message, setMessage] = useState(() => window.sessionStorage.getItem(draftKey) ?? "");
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useLayoutEffect(() => {
    const input = inputRef.current;
    if (input === null) {
      return;
    }
    input.style.height = "auto";
    input.style.height = `${String(Math.max(48, Math.min(input.scrollHeight, 180)))}px`;
    input.style.overflowY = input.scrollHeight > 180 ? "auto" : "hidden";
  }, [message]);

  useLayoutEffect(() => {
    if (message.length === 0) {
      window.sessionStorage.removeItem(draftKey);
    } else {
      window.sessionStorage.setItem(draftKey, message);
    }
  }, [draftKey, message]);

  const submit = () => {
    if (streaming || message.trim().length === 0) {
      return;
    }
    onSend(message);
    setMessage("");
    window.sessionStorage.removeItem(draftKey);
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      submit();
    }
  };

  return (
    <form
      className="chat-composer"
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      <label className="sr-only" htmlFor="chat-message">Nội dung tin nhắn</label>
      <div className="chat-composer-row">
        <textarea
          id="chat-message"
          ref={inputRef}
          maxLength={8000}
          placeholder="Nhập câu hỏi cho KiRa"
          rows={1}
          value={message}
          onChange={(event) => {
            setMessage(event.target.value);
          }}
          onKeyDown={handleKeyDown}
        />
        {streaming ? (
          <button
            className="composer-action stop-action"
            type="button"
            onClick={onStop}
            aria-label="Dừng"
            title="Dừng tạo câu trả lời"
          >
            <span className="stop-icon" aria-hidden="true" />
          </button>
        ) : (
          <button
            className="composer-action send-action"
            type="submit"
            disabled={message.trim().length === 0}
            aria-label="Gửi"
            title="Gửi tin nhắn"
          >
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="M12 19V5m0 0-6 6m6-6 6 6" />
            </svg>
          </button>
        )}
      </div>
      <p className="composer-help">
        KiRa có thể mắc lỗi. Hãy kiểm tra thông tin quan trọng.
        <span>Enter để gửi · Shift + Enter để xuống dòng</span>
      </p>
    </form>
  );
}
