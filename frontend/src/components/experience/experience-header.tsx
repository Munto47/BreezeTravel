'use client'

import { useEffect } from 'react'
import Link from 'next/link'
import { ArrowUpRight, UserRound } from 'lucide-react'
import { useAuthStore } from '@/stores/authStore'
import Brand from './brand'

export default function ExperienceHeader({ active = 'trip' }: {active?: 'trip' | 'guide' | 'my-trips'}) {
  const {user, hydrate} = useAuthStore()
  useEffect(() => hydrate(), [hydrate])
  return <header className="four-site-header">
    <Brand />
    <nav className="four-site-nav" aria-label="全局导航">
      <Link href="/" aria-current={active === 'trip' ? 'page' : undefined}>行程查</Link>
      <Link href="/guide" aria-current={active === 'guide' ? 'page' : undefined}>使用指南</Link>
      <Link href="/my-trips" aria-current={active === 'my-trips' ? 'page' : undefined}>我的行程</Link>
      <Link href="/collaborate" className="four-collaborate-link">协同规划<ArrowUpRight size={14} aria-hidden="true" /></Link>
    </nav>
    <Link href={user ? '/profile' : '/login'} className="four-account-link" onClick={() => {
      if (!user) sessionStorage.removeItem('bt_login_return')
    }}><UserRound size={16} aria-hidden="true" />{user ? '账号' : '登录'}</Link>
  </header>
}
