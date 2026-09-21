import * as Dialog from "@radix-ui/react-dialog";
import { Pencil, X } from "lucide-react";
import { useState, type SyntheticEvent } from "react";

import { FormError } from "../components/FormError";
import type { ConversationSummary } from "./conversationApi";
import { conversationTitle } from "./conversationLabels";
import { useConversations } from "./useConversations";

export function RenameConversationDialog({ conversation, onClose }: {
  conversation: ConversationSummary;
  onClose: () => void;
}) {
  const conversations = useConversations();
  const [title, setTitle] = useState(conversationTitle(conversation));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const submit = async (event: SyntheticEvent<HTMLFormElement>) => {
    event.preventDefault();
    const normalized = title.trim();
    if (normalized.length === 0 || normalized === conversation.title) {
      onClose();
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await conversations.rename(conversation.session_id, normalized);
      onClose();
    } catch (caught) {
      setError(caught);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog.Root open onOpenChange={(open) => {
      if (!open && !saving) {
        onClose();
      }
    }}>
      <Dialog.Portal>
        <Dialog.Overlay className="dialog-backdrop" />
        <Dialog.Content className="confirm-dialog rename-dialog">
          <div className="dialog-icon" aria-hidden="true"><Pencil size={19} /></div>
          <Dialog.Title>Đổi tên cuộc trò chuyện</Dialog.Title>
          <Dialog.Description>Đặt một tên ngắn để dễ tìm lại trong lịch sử.</Dialog.Description>
          <form onSubmit={(event) => void submit(event)}>
            <label className="sr-only" htmlFor="rename-conversation">Tên cuộc trò chuyện</label>
            <input
              id="rename-conversation"
              className="dialog-input"
              value={title}
              maxLength={200}
              autoFocus
              onFocus={(event) => { event.currentTarget.select(); }}
              onChange={(event) => { setTitle(event.target.value); }}
            />
            {error ? <FormError error={error} /> : null}
            <div className="dialog-actions">
              <Dialog.Close asChild>
                <button className="secondary-button" type="button" disabled={saving}>Hủy</button>
              </Dialog.Close>
              <button className="primary-button" type="submit" disabled={saving || title.trim().length === 0}>
                {saving ? "Đang lưu" : "Lưu tên"}
              </button>
            </div>
          </form>
          <Dialog.Close asChild>
            <button className="dialog-close" type="button" aria-label="Đóng" disabled={saving}>
              <X size={18} />
            </button>
          </Dialog.Close>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
