import { useState, type SyntheticEvent } from "react";
import { useNavigate } from "react-router-dom";

import { FormError } from "../components/FormError";
import { useConversations } from "../conversations/useConversations";

export function NewChatPage() {
  const conversations = useConversations();
  const navigate = useNavigate();
  const [title, setTitle] = useState("");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const handleCreate = async (event: SyntheticEvent<HTMLFormElement>) => {
    event.preventDefault();
    setCreating(true);
    setError(null);
    try {
      const normalizedTitle = title.trim();
      const created = await conversations.create(normalizedTitle || undefined);
      void navigate(`/chat/${created.session_id}`);
    } catch (caught) {
      setError(caught);
    } finally {
      setCreating(false);
    }
  };

  return (
    <section className="new-conversation" aria-labelledby="new-chat-title">
      <p className="route-kicker">Cuộc trò chuyện mới</p>
      <h1 id="new-chat-title">Bạn muốn hỏi gì?</h1>
      <p>Đặt tên để dễ tìm lại. Bạn có thể bỏ trống và bắt đầu ngay.</p>
      <form className="new-conversation-form" onSubmit={(event) => void handleCreate(event)}>
        <div className="field-group">
          <label htmlFor="conversation-title">Tên cuộc trò chuyện</label>
          <input
            id="conversation-title"
            name="conversation-title"
            type="text"
            value={title}
            maxLength={200}
            autoComplete="off"
            onChange={(event) => {
              setTitle(event.target.value);
            }}
          />
          <span className="field-help">Tối đa 200 ký tự.</span>
        </div>
        {error ? <FormError error={error} /> : null}
        <button className="primary-button" type="submit" disabled={creating}>
          {creating ? "Đang tạo" : "Bắt đầu trò chuyện"}
        </button>
      </form>
    </section>
  );
}
