import path from 'node:path'
import { defineConfig, type UserConfigExport } from '@tarojs/cli'

export default defineConfig<'webpack5'>(async (merge, { command, mode }) => {
  const base: UserConfigExport<'webpack5'> = {
    projectName: 'breezetravel-miniapp',
    date: '2026-08-26',
    designWidth: 750,
    deviceRatio: { 750: 1 },
    sourceRoot: 'src',
    outputRoot: 'dist',
    framework: 'react',
    compiler: 'webpack5',
    cache: { enable: false },
    alias: {
      '@': path.resolve(__dirname, '..', 'src'),
      '@breezetravel/trip-check-client': path.resolve(__dirname, '..', '..', 'packages', 'trip-check-client', 'src'),
    },
    defineConstants: {
      'process.env.TARO_APP_API_URL': JSON.stringify(process.env.TARO_APP_API_URL || 'http://127.0.0.1:8000'),
    },
    mini: {
      postcss: {
        pxtransform: { enable: true },
        url: { enable: true, config: { limit: 1024 } },
        cssModules: { enable: false },
      },
    },
  }
  return merge({}, base, command === 'build' ? { mode } : {})
})
