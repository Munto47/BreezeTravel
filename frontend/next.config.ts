import type { NextConfig } from 'next'

const nextConfig: NextConfig = {
  // Build a self-contained, content-addressable runtime directory. The
  // release process starts this directory before switching traffic, so an
  // existing process never observes a partially replaced `.next` tree.
  output: 'standalone',
  // 允许高德地图图片域名
  images: {
    domains: ['aos-comment.amap.com', 'p1.meituan.net'],
  },
  // 关闭 webpack 文件系统缓存，避免 Windows 上 rename 文件锁竞争
  webpack: (config, { dev }) => {
    if (dev) {
      config.cache = false
    }
    return config
  },
  async rewrites() {
    // Docker's browser-facing frontend uses this same-origin bridge so it
    // never bakes a transient LAN address into client JavaScript.  An explicit
    // NEXT_PUBLIC_API_URL remains available for non-Docker deployments.
    const backend = process.env.BACKEND_INTERNAL_URL || 'http://localhost:8000'
    return [{ source: '/api/:path*', destination: `${backend}/api/:path*` }]
  },
}

export default nextConfig
