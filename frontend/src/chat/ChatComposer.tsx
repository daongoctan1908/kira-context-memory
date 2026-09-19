import { useState, type KeyboardEvent } from "react";

export function ChatComposer({
  streaming,
  onSend,
  onStop,
}: {
  streaming: boolean;
  onSend: (message: string) => void;
  onStop: () => void;
}) {
  const [message, setMessage] = useState("");

  const submit = () => {
    if (streaming || message.trim().length === 0) {
      return;
    }
    onSend(message);
    setMessage("");
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
      <label htmlFor="chat-message">Nội dung tin nhắn</label>
      <div className="chat-composer-row">
        <textarea
          id="chat-message"
          maxLength={8000}
          placeholder="Nhập câu hỏi cho KiRa"
          rows={2}
          value={message}
          onChange={(event) => {
            setMessage(event.target.value);
          }}
          onKeyDown={handleKeyDown}
        />
        {streaming ? (
          <button className="secondary-button composer-action" type="button" onClick={onStop}>
            Dừng
          </button>
        ) : (
          <button
            className="primary-button composer-action"
            type="submit"
            disabled={message.trim().length === 0}
          >
            Gửi
          </button>
        )}
      </div>
      <p className="composer-help">Enter để gửi, Shift + Enter để xuống dòng.</p>
    </form>
  );
}
