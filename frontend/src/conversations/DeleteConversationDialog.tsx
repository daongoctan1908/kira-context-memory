import * as Dialog from "@radix-ui/react-dialog";
import { AlertTriangle, X } from "lucide-react";

import type { ConversationSummary } from "./conversationApi";
import { conversationTitle } from "./conversationLabels";

export function DeleteConversationDialog({
  conversation,
  busy,
  onCancel,
  onConfirm,
}: {
  conversation: ConversationSummary;
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return (
    <Dialog.Root open onOpenChange={(open) => {
      if (!open && !busy) {
        onCancel();
      }
    }}>
      <Dialog.Portal>
        <Dialog.Overlay className="dialog-backdrop" />
        <Dialog.Content className="confirm-dialog">
          <div className="dialog-icon danger" aria-hidden="true"><AlertTriangle size={20} /></div>
          <Dialog.Title>Xóa cuộc trò chuyện?</Dialog.Title>
          <Dialog.Description>
            Lịch sử và toàn bộ memory sinh từ cuộc trò chuyện này sẽ bị xóa vĩnh viễn.
          </Dialog.Description>
          <p className="dialog-subject">{conversationTitle(conversation)}</p>
          <div className="dialog-actions">
            <Dialog.Close asChild>
              <button className="secondary-button" type="button" disabled={busy}>Hủy</button>
            </Dialog.Close>
            <button className="danger-button" type="button" disabled={busy} onClick={onConfirm}>
              {busy ? "Đang xóa" : "Xóa vĩnh viễn"}
            </button>
          </div>
          <Dialog.Close asChild>
            <button className="dialog-close" type="button" aria-label="Đóng" disabled={busy}>
              <X size={18} />
            </button>
          </Dialog.Close>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
