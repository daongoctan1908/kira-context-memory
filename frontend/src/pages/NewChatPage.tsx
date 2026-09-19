export function NewChatPage() {
  return (
    <section className="empty-conversation" aria-labelledby="new-chat-title">
      <p className="route-kicker">Cuộc trò chuyện mới</p>
      <h1 id="new-chat-title">Bạn muốn hỏi gì?</h1>
      <p>Khung nhập tin nhắn sẽ được kết nối với API ở bước streaming.</p>
    </section>
  );
}
