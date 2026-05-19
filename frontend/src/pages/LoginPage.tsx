import { type FormEvent, useState } from "react";
import { Eye, EyeOff, Lock, Mail, Plane } from "lucide-react";
import { useNavigate } from "react-router-dom";

import { Button } from "../components/ui/Button";
import { useAuth } from "../context/AuthContext";

export function LoginPage() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setLoading(true);

    try {
      await login(email, password);
      navigate("/", { replace: true });
    } catch (err: unknown) {
      const isNetworkError =
        err instanceof TypeError ||
        (err as { response?: unknown })?.response === undefined;

      setError(
        isNetworkError
          ? "Cannot reach the server. Make sure the backend is running."
          : "Invalid email or password. Please try again.",
      );
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="relative min-h-screen overflow-hidden bg-[#f6f8fc]">
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_top,_rgba(59,130,246,0.10),_transparent_36%),radial-gradient(circle_at_bottom,_rgba(99,102,241,0.06),_transparent_34%)]" />

      <header className="relative z-10 border-b border-[#e8ecf4] bg-white/85 backdrop-blur-sm">
        <div className="flex h-[102px] items-center px-8">
          <div className="flex items-center gap-4">
            <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-[#edf2ff] text-brand-600 shadow-[0_20px_40px_-32px_rgba(59,130,246,0.55)]">
              <Plane className="h-7 w-7" />
            </div>
            <div className="text-[19px] font-bold tracking-[-0.02em] text-[#0f172a]">
              Flight Scraper
            </div>
          </div>
        </div>
      </header>

      <main className="relative z-10 flex min-h-[calc(100vh-102px)] flex-col items-center justify-center px-6 py-14">
        <div className="w-full max-w-[510px] rounded-[28px] border border-[#e7ebf3] bg-white px-9 py-10 shadow-[0_28px_90px_-54px_rgba(15,23,42,0.35)]">
          <div className="mb-8 flex flex-col items-center text-center">
            <div className="mb-8 flex h-[68px] w-[68px] items-center justify-center rounded-3xl bg-[#edf2ff] text-brand-600 shadow-[0_20px_40px_-32px_rgba(59,130,246,0.45)]">
              <Plane className="h-8 w-8" />
            </div>
            <h1 className="text-[31px] font-bold tracking-[-0.03em] text-[#0f172a]">
              Welcome back
            </h1>
            <p className="mt-2 text-[15px] text-[#7183a6]">
              Sign in to continue to your account
            </p>
          </div>

          <form onSubmit={handleSubmit} className="space-y-6">
            <div className="space-y-2">
              <label htmlFor="email" className="text-[14px] font-medium text-[#18243d]">
                Email address
              </label>
              <div className="flex h-[52px] items-center overflow-hidden rounded-2xl border border-[#dfe6f0] bg-white px-4 shadow-[0_1px_2px_rgba(15,23,42,0.02)] transition focus-within:border-brand-500 focus-within:ring-2 focus-within:ring-[rgba(37,99,235,0.10)]">
                <Mail className="mr-3 h-[18px] w-[18px] text-[#9aa7be]" />
                <input
                  id="email"
                  type="email"
                  autoComplete="email"
                  required
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  className="auth-input h-full w-full border-none bg-transparent p-0 text-[16px] text-[#0f172a] outline-none placeholder:text-[#94a3b8]"
                  placeholder="admin@example.com"
                />
              </div>
            </div>

            <div className="space-y-2">
              <label htmlFor="password" className="text-[14px] font-medium text-[#18243d]">
                Password
              </label>
              <div className="flex h-[52px] items-center overflow-hidden rounded-2xl border border-[#dfe6f0] bg-white px-4 shadow-[0_1px_2px_rgba(15,23,42,0.02)] transition focus-within:border-brand-500 focus-within:ring-2 focus-within:ring-[rgba(37,99,235,0.10)]">
                <Lock className="mr-3 h-[18px] w-[18px] text-[#9aa7be]" />
                <input
                  id="password"
                  type={showPassword ? "text" : "password"}
                  autoComplete="current-password"
                  required
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  className="auth-input h-full w-full border-none bg-transparent p-0 text-[16px] text-[#0f172a] outline-none placeholder:text-[#94a3b8]"
                  placeholder="••••••••"
                />
                <button
                  type="button"
                  onClick={() => setShowPassword((current) => !current)}
                  className="ml-3 inline-flex h-8 w-8 items-center justify-center rounded-full text-[#9aa7be] transition hover:bg-slate-50 hover:text-[#64748b]"
                  aria-label={showPassword ? "Hide password" : "Show password"}
                >
                  {showPassword ? <EyeOff className="h-[18px] w-[18px]" /> : <Eye className="h-[18px] w-[18px]" />}
                </button>
              </div>
            </div>

            {error ? (
              <div className="rounded-2xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-600">
                {error}
              </div>
            ) : null}

            <Button
              type="submit"
              variant="primary"
              loading={loading}
              className="h-[54px] w-full rounded-2xl bg-[#2157f3] text-[20px] font-semibold shadow-[0_18px_40px_-26px_rgba(37,99,235,0.75)] hover:bg-[#1c4de0]"
            >
              Sign in
            </Button>
          </form>
        </div>

        <p className="mt-16 text-center text-[13px] text-[#8a96aa]">
          © 2026 Flight Scraper. All rights reserved.
        </p>
      </main>
    </div>
  );
}
