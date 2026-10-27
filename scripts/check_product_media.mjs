#!/usr/bin/env node
// 防止页面发布早于演示封面/视频生成，导致 <img> 404 或视频空白。
import { readFileSync, statSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
const root = new URL('../frontend/public/media/product/', import.meta.url)
const manifest = JSON.parse(readFileSync(new URL('manifest.json', root), 'utf8'))
const required = ['workbench-desktop.webp', 'dashboard-dark.webp', 'assistant-process.webp', 'system-light.webp', 'mobile-assistant.webp', 'preview.gif', ...['market', 'assistant', 'system', 'mobile'].flatMap(name => [`${name}.mp4`, `${name}.zh.vtt`]), ...['market', 'assistant', 'system'].map(name => `${name}-poster.webp`)]
const names = new Set([...required, ...manifest.files.map(file => file.file)])
for (const name of names) {
  const entry = manifest.files.find(file => file.file === name)
  if (!entry) throw new Error(`Product media missing from manifest: ${name}`)
  const path = new URL(name, root)
  if (statSync(path).size !== entry.bytes || entry.bytes === 0) throw new Error(`Product media is missing, empty or changed: ${fileURLToPath(path)}`)
  const data = readFileSync(path)
  if (name.endsWith('.webp') && (data.toString('ascii', 0, 4) !== 'RIFF' || data.toString('ascii', 8, 12) !== 'WEBP')) throw new Error(`Invalid WebP: ${name}`)
  if (name.endsWith('.vtt') && !data.toString('utf8').startsWith('WEBVTT')) throw new Error(`Invalid captions: ${name}`)
  if (name.endsWith('.mp4') && data.toString('ascii', 4, 8) !== 'ftyp') throw new Error(`Invalid MP4: ${name}`)
}
console.log(`Product media OK: ${names.size} files with posters, videos and captions`)
