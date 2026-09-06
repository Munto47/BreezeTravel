const base = require('./playwright.quality-live.config')
module.exports = { ...base, testMatch: 'place-accuracy-live.spec.js' }
