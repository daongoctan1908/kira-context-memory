import { useState, type SyntheticEvent } from "react";

import { useAuth } from "../auth/useAuth";
import { FormError } from "../components/FormError";

export function LoginPage() {
  const auth = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const handleSubmit = async (event: SyntheticEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await auth.login({ username, password });
    } catch (caught) {
      setError(caught);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <main className="centered-page" id="main-content">
      <section className="route-panel auth-panel" aria-labelledby="login-title">
        <span className="brand-mark brand-mark-large" aria-hidden="true">
          K
        </span>
        <p className="route-kicker">KiRa Chat</p>
        <h1 id="login-title">Truy cập trợ lý nội bộ</h1>
        <p className="route-copy">Đăng nhập bằng tài khoản được quản trị viên cấp.</p>
        {auth.notice === "passwordChanged" ? (
          <p className="form-success" role="status">
            Mật khẩu đã được đổi. Hãy đăng nhập lại.
          </p>
        ) : null}
        {auth.notice === "signedOut" ? (
          <p className="form-success" role="status">
            Bạn đã đăng xuất an toàn.
          </p>
        ) : null}
        <form
          className="auth-form"
          onSubmit={(event) => {
            void handleSubmit(event);
          }}
        >
          <div className="field-group">
            <label htmlFor="username">Tên đăng nhập</label>
            <input
              id="username"
              name="username"
              type="text"
              autoComplete="username"
              value={username}
              onChange={(event) => {
                setUsername(event.target.value);
              }}
              minLength={3}
              maxLength={64}
              required
              autoFocus
            />
          </div>
          <div className="field-group">
            <label htmlFor="password">Mật khẩu</label>
            <input
              id="password"
              name="password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => {
                setPassword(event.target.value);
              }}
              maxLength={128}
              required
            />
          </div>
          {error ? <FormError error={error} /> : null}
          <button className="primary-button" type="submit" disabled={submitting}>
            {submitting ? "Đang đăng nhập" : "Đăng nhập"}
          </button>
        </form>
      </section>
    </main>
  );
}
