const { defineConfig } = require('@playwright/test')
const { execFileSync } = require('node:child_process')
const os = require('node:os')
const path = require('node:path')
const productDelivery = require('./playwright.product-delivery.config')

const commitSha = execFileSync('git', ['rev-parse', 'HEAD'], {
  cwd: path.resolve(__dirname, '..'),
  encoding: 'utf8',
}).trim()
const reportPath = process.env.G07_G5_PLAYWRIGHT_JSON
const configuredOutput = process.env.G07_G5_PLAYWRIGHT_OUTPUT

if (reportPath && !path.isAbsolute(reportPath)) {
  throw new Error('G07_G5_PLAYWRIGHT_JSON_ABSOLUTE_REQUIRED')
}
if (configuredOutput && !path.isAbsolute(configuredOutput)) {
  throw new Error('G07_G5_PLAYWRIGHT_OUTPUT_ABSOLUTE_REQUIRED')
}
if (process.env.G07_CANDIDATE_COMMIT && process.env.G07_CANDIDATE_COMMIT !== commitSha) {
  throw new Error('G07_CANDIDATE_COMMIT_MISMATCH')
}

module.exports = defineConfig({
  ...productDelivery,
  testMatch: [
    'trip-understanding-v3.spec.js',
    'g02-product-delivery.spec.js',
    'g03-product-delivery.spec.js',
    'g03r-result-ui.spec.js',
    'g04-screenshot-parity.spec.js',
    'g05-knowledge.spec.js',
    'g06-memory-share.spec.js',
  ],
  metadata: {
    commit_sha: commitSha,
    evidence_class: 'CONTROLLED_BROWSER_FIXTURE',
    evidence_scope: 'G07_G5_FULL_PRODUCT_CHAIN',
    live_provider_evidence: false,
    public_e2e_evidence: false,
    human_evidence: false,
  },
  outputDir: configuredOutput || path.join(os.tmpdir(), 'breezetravel-g07-playwright', commitSha),
  reporter: reportPath ? [['list'], ['json', { outputFile: reportPath }]] : [['list']],
})
