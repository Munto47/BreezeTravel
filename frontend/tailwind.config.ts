import type { Config } from 'tailwindcss'

const config: Config = {
  content: [
    './src/pages/**/*.{js,ts,jsx,tsx,mdx}',
    './src/components/**/*.{js,ts,jsx,tsx,mdx}',
    './src/app/**/*.{js,ts,jsx,tsx,mdx}',
  ],
  theme: {
    extend: {
      colors: {
        // 珊瑚红主色系 —— Airbnb 风格暖色调
        coral: {
          50:  '#fff5f5',
          100: '#ffe0e0',
          200: '#ffbdbd',
          300: '#ff9b9b',
          400: '#ff7070',
          500: '#FF5A5F',  // 主色
          600: '#e04850',
          700: '#c03540',
          800: '#9c2a33',
          900: '#7a2029',
        },
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', '-apple-system', 'sans-serif'],
      },
      borderRadius: {
        'glass': '12px',
      },
      boxShadow: {
        'glass': '0 8px 32px rgba(0, 0, 0, 0.08)',
        'glass-lg': '0 12px 48px rgba(0, 0, 0, 0.12)',
        'glass-hover': '0 12px 40px rgba(0, 0, 0, 0.15)',
        'card': '0 2px 12px rgba(0, 0, 0, 0.06)',
        'card-hover': '0 4px 20px rgba(0, 0, 0, 0.1)',
      },
      backdropBlur: {
        'glass': '16px',
      },
      animation: {
        'pulse-dot': 'pulse-dot 1.5s ease-in-out infinite',
        'slide-up': 'slide-up 0.3s ease-out',
        'slide-in-left': 'slide-in-left 0.3s ease-out',
        'slide-in-right': 'slide-in-right 0.3s ease-out',
        'fade-in': 'fade-in 0.2s ease-out',
        'fade-in-fast': 'fade-in 0.15s ease-out',
      },
      keyframes: {
        'pulse-dot': {
          '0%, 100%': { opacity: '0.4', transform: 'scale(1)' },
          '50%': { opacity: '1', transform: 'scale(1.2)' },
        },
        'slide-up': {
          '0%': { opacity: '0', transform: 'translateY(12px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        'slide-in-left': {
          '0%': { opacity: '0', transform: 'translateX(-20px)' },
          '100%': { opacity: '1', transform: 'translateX(0)' },
        },
        'slide-in-right': {
          '0%': { opacity: '0', transform: 'translateX(20px)' },
          '100%': { opacity: '1', transform: 'translateX(0)' },
        },
        'fade-in': {
          '0%': { opacity: '0' },
          '100%': { opacity: '1' },
        },
      },
    },
  },
  plugins: [],
}

export default config
