import { ArrowUp, Globe2, RadioTower, Sparkles } from "lucide-react";
import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent, type SyntheticEvent } from "react";
import { useNavigate } from "react-router-dom";

import { FormError } from "../components/FormError";
import { useConversations } from "../conversations/useConversations";

const NEW_CHAT_DRAFT_KEY = "kira-chat-draft:new";

export function NewChatPage() {
  const conversations = useConversations();
  const navigate = useNavigate();
  const [prompt, setPrompt] = useState(() => window.sessionStorage.getItem(NEW_CHAT_DRAFT_KEY) ?? "");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const promptInput = useRef<HTMLTextAreaElement>(null);

  useLayoutEffect(() => {
    const input = promptInput.current;
    if (input === null) return;
    input.style.height = "auto";
    input.style.height = `${String(Math.max(54, Math.min(input.scrollHeight, 180)))}px`;
  }, [prompt]);

  useEffect(() => {
    if (prompt.length === 0) window.sessionStorage.removeItem(NEW_CHAT_DRAFT_KEY);
    else window.sessionStorage.setItem(NEW_CHAT_DRAFT_KEY, prompt);
  }, [prompt]);

  const handleCreate = async (event: SyntheticEvent<HTMLFormElement>) => {
    event.preventDefault();
    const normalizedPrompt = prompt.trim();
    if (normalizedPrompt.length === 0) return;
    setCreating(true);
    setError(null);
    try {
      const created = await conversations.create(titleFromPrompt(normalizedPrompt));
      window.sessionStorage.removeItem(NEW_CHAT_DRAFT_KEY);
      void navigate(`/chat/${created.session_id}`, { state: { initialMessage: normalizedPrompt } });
    } catch (caught) {
      setError(caught);
    } finally {
      setCreating(false);
    }
  };

  const handlePromptKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing && !creating) {
      event.preventDefault();
      event.currentTarget.form?.requestSubmit();
    }
  };

  return (
    <section className="new-conversation" aria-labelledby="new-chat-title">
      <div className="new-chat-hero">
        <span className="hero-mark" aria-hidden="true"><Sparkles size={25} /></span>
        <h2 id="new-chat-title">Hôm nay tôi có thể giúp gì cho bạn?</h2>
        <p>Hỏi KiRa về sản phẩm, chính sách hoặc nghiệp vụ viễn thông nội bộ.</p>
      </div>
      <form className="new-conversation-form" onSubmit={(event) => void handleCreate(event)}>
        <div className="new-chat-prompt-shell">
          <label className="sr-only" htmlFor="initial-message">Tin nhắn đầu tiên</label>
          <textarea
            id="initial-message"
            ref={promptInput}
            rows={1}
            maxLength={8000}
            placeholder="Nhập câu hỏi cho KiRa"
            value={prompt}
            onChange={(event) => { setPrompt(event.target.value); }}
            onKeyDown={handlePromptKeyDown}
            autoFocus
          />
          <button className="new-chat-send" type="submit" disabled={creating || prompt.trim().length === 0} aria-label="Bắt đầu trò chuyện" title="Bắt đầu trò chuyện">
            {creating ? <span className="button-spinner" aria-hidden="true" /> : <ArrowUp size={20} />}
          </button>
        </div>
        <p className="new-chat-help">KiRa có thể mắc lỗi. Hãy kiểm tra thông tin quan trọng.</p>
        {error ? <FormError error={error} /> : null}
      </form>
      <div className="prompt-suggestions" aria-label="Câu hỏi gợi ý">
        {STARTER_PROMPTS.map((suggestion) => (
          <button key={suggestion.text} type="button" onClick={() => {
            setPrompt(suggestion.text);
            promptInput.current?.focus();
          }}>
            {suggestion.icon}
            <span>{suggestion.text}</span>
          </button>
        ))}
      </div>
    </section>
  );
}

const STARTER_PROMPTS = [
  { text: "Tư vấn gói cước phù hợp với nhu cầu của tôi", icon: <RadioTower size={18} /> },
  { text: "Hướng dẫn bật chuyển vùng quốc tế", icon: <Globe2 size={18} /> },
  { text: "Tóm tắt chính sách dành cho khách hàng doanh nghiệp", icon: <Sparkles size={18} /> },
];

function titleFromPrompt(prompt: string): string {
  return prompt.length <= 72 ? prompt : `${prompt.slice(0, 69).trimEnd()}…`;
}
