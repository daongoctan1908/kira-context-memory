import { useParams } from "react-router-dom";

export function ConversationPage() {
  const { sessionId } = useParams<{ sessionId: string }>();

  return (
    <section className="empty-conversation" aria-labelledby="conversation-title">
      <p className="route-kicker">Lịch sử trò chuyện</p>
      <h1 id="conversation-title">Cuộc trò chuyện</h1>
      <p>
        Đang chuẩn bị dữ liệu cho phiên <code>{sessionId ?? "không xác định"}</code>.
      </p>
    </section>
  );
}
