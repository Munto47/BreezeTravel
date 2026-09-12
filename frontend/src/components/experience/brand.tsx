import Link from 'next/link'

export default function Brand() {
  return <Link href="/" className="four-brand" aria-label="行程查首页">
    <svg viewBox="0 0 32 40" fill="none" aria-hidden="true"><path d="M16 1C7.7 1 1 7.6 1 15.8c0 10.4 15 23.2 15 23.2s15-12.8 15-23.2C31 7.6 24.3 1 16 1Z" fill="currentColor"/><circle cx="16" cy="15" r="6.5" fill="white"/></svg>
    <strong>行程查</strong><span>TRIPCHECK</span>
  </Link>
}
