import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

// Protects /app/* by asking Django who holds the session cookie.
// The login page itself stays public.
export async function middleware(request: NextRequest) {
  if (request.nextUrl.pathname === "/app/login") {
    return NextResponse.next();
  }
  const backend =
    process.env.OFFERFLOW_BACKEND_URL ?? "http://backend:8000";
  try {
    const me = await fetch(`${backend}/api/orgs/auth/me/`, {
      headers: { cookie: request.headers.get("cookie") ?? "" },
    });
    if (me.ok) return NextResponse.next();
  } catch {
    // Backend unreachable: let the page render its own error state.
    return NextResponse.next();
  }
  const loginUrl = new URL("/app/login", request.url);
  loginUrl.searchParams.set("next", request.nextUrl.pathname);
  return NextResponse.redirect(loginUrl);
}

export const config = {
  matcher: ["/app/:path*"],
};
