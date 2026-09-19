import { ApiError } from "../api/http";

const ERROR_MESSAGES: Readonly<Record<string, string>> = {
  AUTH_INVALID_CREDENTIALS: "Tên đăng nhập hoặc mật khẩu không đúng.",
  AUTH_CSRF_INVALID: "Yêu cầu đã hết hiệu lực. Hãy tải lại trang và thử lại.",
  AUTH_CSRF_MISSING: "Không tìm thấy mã bảo vệ phiên. Hãy đăng nhập lại.",
  AUTH_PASSWORD_POLICY: "Mật khẩu mới phải có từ 12 đến 128 ký tự.",
  AUTH_SESSION_INVALID: "Phiên đăng nhập đã hết hạn. Hãy đăng nhập lại.",
  AUTH_UNAVAILABLE: "Dịch vụ xác thực đang tạm thời không khả dụng.",
  NETWORK_ERROR: "Không thể kết nối tới máy chủ. Hãy kiểm tra kết nối và thử lại.",
};

export function FormError({ error }: { error: unknown }) {
  const apiError = error instanceof ApiError ? error : null;
  const message =
    (apiError === null ? undefined : ERROR_MESSAGES[apiError.code]) ??
    "Không thể hoàn tất yêu cầu. Hãy thử lại.";

  return (
    <div className="form-error" role="alert">
      <strong>{message}</strong>
      {apiError?.correlationId ? <span>Mã hỗ trợ: {apiError.correlationId}</span> : null}
    </div>
  );
}
