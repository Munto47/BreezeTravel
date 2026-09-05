import { create } from 'zustand'
import { clearBrowserAuth } from '@/lib/request-safety'

interface AuthUser {
  userId: string
  nickname: string
  avatarUrl?: string
}

interface AuthStore {
  user: AuthUser | null
  token: string | null
  isHydrated: boolean
  login: (token: string, user: AuthUser) => void
  logout: () => void
  updateUser: (partial: Partial<AuthUser>) => void
  hydrate: () => void
}

export const useAuthStore = create<AuthStore>((set, get) => ({
  user: null,
  token: null,
  isHydrated: false,

  hydrate: () => {
    if (typeof window === 'undefined') return
    const token = localStorage.getItem('authToken')
    const raw = localStorage.getItem('authUser')
    if (token && raw) {
      try {
        const user = JSON.parse(raw) as Partial<AuthUser>
        if (
          typeof user.userId === 'string' &&
          user.userId.length > 0 &&
          typeof user.nickname === 'string' &&
          user.nickname.length > 0
        ) {
          set({ user: user as AuthUser, token, isHydrated: true })
          return
        }
      } catch {}
    }
    clearBrowserAuth()
    set({ user: null, token: null, isHydrated: true })
  },

  login: (token, user) => {
    localStorage.setItem('authToken', token)
    localStorage.setItem('authUser', JSON.stringify(user))
    // 兼容旧代码：同步写 userId / nickname 到 localStorage
    localStorage.setItem('userId', user.userId)
    localStorage.setItem('nickname', user.nickname)
    set({ token, user })
  },

  logout: () => {
    clearBrowserAuth()
    set({ token: null, user: null })
    window.location.assign('/login')
  },

  updateUser: (partial) => {
    const current = get().user
    if (!current) return
    const updated = { ...current, ...partial }
    localStorage.setItem('authUser', JSON.stringify(updated))
    localStorage.setItem('nickname', updated.nickname)
    set({ user: updated })
  },
}))
