import crypto from "node:crypto";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";

const DEFAULT_TOKEN_PATH = path.join(os.homedir(), ".trading-assistant", "tradovate-token.enc");

export function tokenStoragePath(env = process.env) {
  return env.TRADOVATE_TOKEN_STORE || DEFAULT_TOKEN_PATH;
}

export function deriveEncryptionKey(secret) {
  if (!secret || secret.length < 16) {
    throw new Error("TRADOVATE_TOKEN_ENCRYPTION_KEY must be set to at least 16 characters for encrypted token storage.");
  }
  return crypto.createHash("sha256").update(secret).digest();
}

export function encryptJson(value, secret) {
  const iv = crypto.randomBytes(12);
  const key = deriveEncryptionKey(secret);
  const cipher = crypto.createCipheriv("aes-256-gcm", key, iv);
  const plaintext = Buffer.from(JSON.stringify(value), "utf8");
  const encrypted = Buffer.concat([cipher.update(plaintext), cipher.final()]);
  const tag = cipher.getAuthTag();
  return Buffer.concat([iv, tag, encrypted]).toString("base64url");
}

export function decryptJson(payload, secret) {
  const raw = Buffer.from(payload, "base64url");
  const iv = raw.subarray(0, 12);
  const tag = raw.subarray(12, 28);
  const encrypted = raw.subarray(28);
  const key = deriveEncryptionKey(secret);
  const decipher = crypto.createDecipheriv("aes-256-gcm", key, iv);
  decipher.setAuthTag(tag);
  const plaintext = Buffer.concat([decipher.update(encrypted), decipher.final()]);
  return JSON.parse(plaintext.toString("utf8"));
}

export function isTokenUsable(tokenInfo, now = Date.now()) {
  if (!tokenInfo?.accessToken || !tokenInfo?.expirationTime) return false;
  const expiresAt = Date.parse(tokenInfo.expirationTime);
  return Number.isFinite(expiresAt) && now < expiresAt - 5 * 60 * 1000;
}

export class EncryptedTokenStore {
  constructor({ storagePath = tokenStoragePath(), encryptionKey = process.env.TRADOVATE_TOKEN_ENCRYPTION_KEY } = {}) {
    this.storagePath = storagePath;
    this.encryptionKey = encryptionKey;
  }

  async load() {
    const encrypted = await fs.readFile(this.storagePath, "utf8").catch(() => null);
    if (!encrypted) return null;
    return decryptJson(encrypted, this.encryptionKey);
  }

  async save(tokenInfo) {
    await fs.mkdir(path.dirname(this.storagePath), { recursive: true, mode: 0o700 });
    const encrypted = encryptJson({ ...tokenInfo, storedAt: new Date().toISOString() }, this.encryptionKey);
    await fs.writeFile(this.storagePath, encrypted, { mode: 0o600 });
  }

  async clear() {
    await fs.rm(this.storagePath, { force: true });
  }
}
