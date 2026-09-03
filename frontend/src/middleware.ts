import { NextRequest, NextResponse } from 'next/server'


export function middleware(request: NextRequest) {
  if (process.env.LEGACY_IMPORT_DIAGNOSTICS_ENABLED === 'true') {
    return NextResponse.next()
  }
  return NextResponse.redirect(new URL('/', request.url), 307)
}


export const config = {
  matcher: [
    '/history/:path*',
    '/import/:path*',
    '/intake/:path*',
    '/room/:path*',
    '/templates/:path*',
    '/workspace/:path*',
  ],
}
