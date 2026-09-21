import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { MoreHorizontal, Pencil, Trash2 } from "lucide-react";
import { useState } from "react";
import { useNavigate } from "react-router-dom";

import type { ConversationSummary } from "./conversationApi";
import { conversationTitle } from "./conversationLabels";
import { DeleteConversationDialog } from "./DeleteConversationDialog";
import { RenameConversationDialog } from "./RenameConversationDialog";
import { useConversations } from "./useConversations";

export function ConversationActions({ conversation, afterDelete }: {
  conversation: ConversationSummary;
  afterDelete?: () => void;
}) {
  const conversations = useConversations();
  const navigate = useNavigate();
  const [dialog, setDialog] = useState<"rename" | "delete" | null>(null);
  const deleting = conversations.deleting.has(conversation.session_id);

  const confirmDelete = async () => {
    try {
      await conversations.remove(conversation.session_id);
      setDialog(null);
      afterDelete?.();
      if (afterDelete === undefined) {
        void navigate("/chat/new", { replace: true });
      }
    } catch {
      setDialog(null);
    }
  };

  return (
    <>
      <DropdownMenu.Root>
        <DropdownMenu.Trigger asChild>
          <button className="icon-button conversation-menu-trigger" type="button" aria-label={`Tùy chọn cho ${conversationTitle(conversation)}`}>
            <MoreHorizontal size={18} />
          </button>
        </DropdownMenu.Trigger>
        <DropdownMenu.Portal>
          <DropdownMenu.Content className="dropdown-menu" sideOffset={6} align="end">
            <DropdownMenu.Item className="dropdown-item" onSelect={() => { setDialog("rename"); }}>
              <Pencil size={16} /> Đổi tên
            </DropdownMenu.Item>
            <DropdownMenu.Separator className="dropdown-separator" />
            <DropdownMenu.Item className="dropdown-item danger" onSelect={() => { setDialog("delete"); }}>
              <Trash2 size={16} /> Xóa
            </DropdownMenu.Item>
          </DropdownMenu.Content>
        </DropdownMenu.Portal>
      </DropdownMenu.Root>
      {dialog === "rename" ? <RenameConversationDialog conversation={conversation} onClose={() => { setDialog(null); }} /> : null}
      {dialog === "delete" ? (
        <DeleteConversationDialog
          conversation={conversation}
          busy={deleting}
          onCancel={() => { setDialog(null); }}
          onConfirm={() => void confirmDelete()}
        />
      ) : null}
    </>
  );
}
