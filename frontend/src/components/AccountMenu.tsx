import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import {
  Check,
  ChevronUp,
  KeyRound,
  LogOut,
  Monitor,
  Moon,
  Sun,
} from "lucide-react";
import { NavLink } from "react-router-dom";

import { useAuth } from "../auth/useAuth";
import { useTheme } from "../theme/useTheme";

export function AccountMenu({
  loggingOut,
  onLogout,
}: {
  loggingOut: boolean;
  onLogout: () => void;
}) {
  const auth = useAuth();
  const theme = useTheme();
  const username = auth.user?.username ?? "Tài khoản";

  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button className="account-trigger" type="button" aria-label="Mở menu tài khoản">
          <span className="account-avatar" aria-hidden="true">{username.slice(0, 1).toUpperCase()}</span>
          <span className="account-trigger-copy">
            <strong>{username}</strong>
            <small>Tài khoản nội bộ</small>
          </span>
          <ChevronUp className="account-chevron" size={16} aria-hidden="true" />
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content className="dropdown-menu account-dropdown" side="top" sideOffset={8} align="start">
          <div className="dropdown-label">Giao diện</div>
          <DropdownMenu.RadioGroup
            value={theme.preference}
            onValueChange={(value) => {
              if (value === "light" || value === "dark" || value === "system") {
                theme.setPreference(value);
              }
            }}
          >
            <ThemeItem value="light" label="Sáng" icon={<Sun size={16} />} />
            <ThemeItem value="dark" label="Tối" icon={<Moon size={16} />} />
            <ThemeItem value="system" label="Theo hệ thống" icon={<Monitor size={16} />} />
          </DropdownMenu.RadioGroup>
          <DropdownMenu.Separator className="dropdown-separator" />
          <DropdownMenu.Item asChild className="dropdown-item">
            <NavLink to="/account/password"><KeyRound size={16} /> Đổi mật khẩu</NavLink>
          </DropdownMenu.Item>
          <DropdownMenu.Item className="dropdown-item danger" disabled={loggingOut} onSelect={onLogout}>
            <LogOut size={16} /> {loggingOut ? "Đang đăng xuất" : "Đăng xuất"}
          </DropdownMenu.Item>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}

function ThemeItem({ value, label, icon }: { value: string; label: string; icon: React.ReactNode }) {
  return (
    <DropdownMenu.RadioItem className="dropdown-item theme-item" value={value}>
      {icon}
      <span>{label}</span>
      <DropdownMenu.ItemIndicator className="menu-check"><Check size={15} /></DropdownMenu.ItemIndicator>
    </DropdownMenu.RadioItem>
  );
}
