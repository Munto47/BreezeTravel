let fallbackCounter = 0

function fallbackBytes(bytes: Uint8Array) {
  fallbackCounter += 1
  let seed = (Date.now() ^ fallbackCounter) >>> 0
  for (let index = 0; index < bytes.length; index += 1) {
    seed = Math.imul(seed ^ (seed >>> 15), 2246822519) >>> 0
    bytes[index] = (seed ^ Math.floor(Math.random() * 256)) & 0xff
  }
}

/**
 * Generate a UUID in secure and non-secure browser contexts.
 *
 * `crypto.randomUUID` is restricted to secure contexts in Chromium. Local
 * device testing commonly uses a LAN HTTP address, where `getRandomValues`
 * remains available. The final fallback only supports non-security command
 * and idempotency identifiers in older embedded WebViews.
 */
export function randomUuid(): string {
  const webCrypto = globalThis.crypto
  if (typeof webCrypto?.randomUUID === 'function') return webCrypto.randomUUID()

  const bytes = new Uint8Array(16)
  if (typeof webCrypto?.getRandomValues === 'function') {
    webCrypto.getRandomValues(bytes)
  } else {
    fallbackBytes(bytes)
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = Array.from(bytes, value => value.toString(16).padStart(2, '0'))
  return [
    hex.slice(0, 4).join(''),
    hex.slice(4, 6).join(''),
    hex.slice(6, 8).join(''),
    hex.slice(8, 10).join(''),
    hex.slice(10, 16).join(''),
  ].join('-')
}
