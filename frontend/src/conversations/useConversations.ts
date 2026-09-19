import { useContext } from "react";

import { ConversationContext } from "./ConversationContext";

export function useConversations() {
  const value = useContext(ConversationContext);
  if (value === null) {
    throw new Error("useConversations must be used inside ConversationProvider");
  }
  return value;
}
