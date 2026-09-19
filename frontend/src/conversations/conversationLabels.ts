import type { ConversationSummary } from "./conversationApi";

export function conversationTitle(conversation: ConversationSummary): string {
  return conversation.title ?? "Cuộc trò chuyện chưa đặt tên";
}
