'use client'

import { useState, useRef, useEffect } from 'react'
import { useRouter } from 'next/navigation'
import { User, History, LogOut, ChevronDown } from 'lucide-react'
import { useAuthStore } from '@/stores/authStore'

export function UserMenu() {
  const router = useRouter()
  const { user, logout } = useAuthStore()
  const [open, setOpen] = useState(false)
  const menuRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const handle = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', handle)
    return () => document.removeEventListener('mousedown', handle)
  }, [])

  if (!user) return null

  const initial = (user.nickname || '?')[0].toUpperCase()

  return (
    <div className="relative" ref={menuRef}>
      <button
        onClick={() => setOpen(o => !o)}
        className="flex items-center gap-1.5 hover:opacity-80 transition-opacity"
      >
        {/* 头像 */}
        <div className="w-8 h-8 rounded-full bg-coral-100 flex items-center justify-center overflow-hidden ring-2 ring-white shadow-sm">
          {user.avatarUrl ? (
            <img src={user.avatarUrl} alt="avatar" className="w-full h-full object-cover" />
          ) : (
            <span className="text-xs font-bold text-coral-500">{initial}</span>
          )}
        </div>
        <span className="text-xs text-gray-600 font-medium max-w-[60px] truncate hidden sm:block">
          {user.nickname}
        </span>
        <ChevronDown className={`w-3 h-3 text-gray-400 transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>

      {/* 下拉菜单 */}
      {open && (
        <div className="absolute right-0 top-full mt-2 w-44 bg-white rounded-xl shadow-lg border border-gray-100 py-1 z-50 animate-[fade-in_0.15s_ease-out]">
          <div className="px-3 py-2 border-b border-gray-50">
            <p className="text-xs font-semibold text-gray-800 truncate">{user.nickname}</p>
          </div>
          <MenuItem
            icon={<User className="w-3.5 h-3.5" />}
            label="个人信息"
            onClick={() => { setOpen(false); router.push('/profile') }}
          />
          <MenuItem
            icon={<History className="w-3.5 h-3.5" />}
            label="旅行历史"
            onClick={() => { setOpen(false); router.push('/history') }}
          />
          <div className="border-t border-gray-50 mt-1 pt-1">
            <MenuItem
              icon={<LogOut className="w-3.5 h-3.5" />}
              label="退出登录"
              onClick={() => { setOpen(false); logout() }}
              danger
            />
          </div>
        </div>
      )}
    </div>
  )
}

function MenuItem({
  icon, label, onClick, danger,
}: {
  icon: React.ReactNode
  label: string
  onClick: () => void
  danger?: boolean
}) {
  return (
    <button
      onClick={onClick}
      className={`w-full flex items-center gap-2.5 px-3 py-2 text-xs font-medium transition-colors ${
        danger ? 'text-red-400 hover:bg-red-50' : 'text-gray-600 hover:bg-gray-50'
      }`}
    >
      {icon}
      {label}
    </button>
  )
}
