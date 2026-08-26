const defineJestConfig = require('@tarojs/test-utils-react/dist/jest').default

module.exports = defineJestConfig({
  testMatch: ['<rootDir>/__test__/**/*.test.{ts,tsx}'],
  moduleNameMapper: {
    '^@/(.*)$': '<rootDir>/src/$1',
    '^@breezetravel/trip-check-client$': '<rootDir>/../packages/trip-check-client/dist/index.js',
    '^@babel/runtime/(.*)$': '<rootDir>/node_modules/@babel/runtime/$1',
    '\\.(scss|css)$': '<rootDir>/__test__/style-mock.js',
  },
})
