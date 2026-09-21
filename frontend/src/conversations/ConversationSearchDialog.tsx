import * as Dialog from "@radix-ui/react-dialog";
import { Clock3, Search, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import { FormError } from "../components/FormError";
import {
  listConversations,
  type ConversationSummary,
} from "./conversationApi";
import { conversationTitle } from "./conversationLabels";

export function ConversationSearchDialog({
  open,
  recent,
  onOpenChange,
}: {
  open: boolean;
  recent: ConversationSummary[];
  onOpenChange: (open: boolean) => void;
}) {
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<ConversationSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const normalized = query.trim();

  useEffect(() => {
    if (!open || normalized.length === 0) {
      return undefined;
    }
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      setLoading(true);
      setError(null);
      void listConversations({ query: normalized, limit: 30, signal: controller.signal })
        .then((page) => { setResults(page.items); })
        .catch((caught: unknown) => {
          if (!(caught instanceof DOMException && caught.name === "AbortError")) {
            setError(caught);
          }
        })
        .finally(() => {
          if (!controller.signal.aborted) {
            setLoading(false);
          }
        });
    }, 250);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [normalized, open]);

  const visible = useMemo(
    () => normalized.length === 0 ? recent.slice(0, 8) : results,
    [normalized.length, recent, results],
  );
  const displayLoading = normalized.length > 0 && loading;
  const displayError = normalized.length > 0 ? error : null;

  const openConversation = (sessionId: string) => {
    onOpenChange(false);
    setQuery("");
    void navigate(`/chat/${sessionId}`);
  };

  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="dialog-backdrop search-backdrop" />
        <Dialog.Content className="search-dialog" aria-describedby={undefined}>
          <Dialog.Title className="sr-only">Tìm cuộc trò chuyện</Dialog.Title>
          <div className="search-input-shell">
            <Search size={19} aria-hidden="true" />
            <input
              aria-label="Tìm cuộc trò chuyện"
              placeholder="Tìm theo tên cuộc trò chuyện"
              value={query}
              maxLength={100}
              autoFocus
              onChange={(event) => {
                const nextQuery = event.target.value;
                setQuery(nextQuery);
                setResults([]);
                setError(null);
                setLoading(nextQuery.trim().length > 0);
              }}
            />
            <kbd>Esc</kbd>
          </div>
          <div className="search-results" aria-live="polite">
            <p className="search-section-label">
              {normalized.length === 0 ? "Gần đây" : "Kết quả"}
            </p>
            {displayLoading ? <div className="search-loading"><span /><span /><span /></div> : null}
            {displayError ? <FormError error={displayError} /> : null}
            {!displayLoading && displayError === null && visible.length === 0 ? (
              <div className="search-empty">
                <Search size={22} />
                <p>Không tìm thấy cuộc trò chuyện phù hợp.</p>
              </div>
            ) : null}
            {visible.length > 0 ? (
              <ul>
                {visible.map((conversation) => (
                  <li key={conversation.session_id}>
                    <button type="button" onClick={() => { openConversation(conversation.session_id); }}>
                      <Clock3 size={17} />
                      <span>
                        <strong>{conversationTitle(conversation)}</strong>
                        <small>{formatActivity(conversation.last_message_at ?? conversation.created_at)}</small>
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            ) : null}
          </div>
          <Dialog.Close asChild>
            <button className="dialog-close search-close" type="button" aria-label="Đóng tìm kiếm">
              <X size={18} />
            </button>
          </Dialog.Close>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function formatActivity(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? ""
    : new Intl.DateTimeFormat("vi-VN", { day: "2-digit", month: "2-digit", year: "numeric" }).format(parsed);
}
