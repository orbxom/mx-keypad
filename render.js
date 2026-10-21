// Draws one 118x118 key image: background, optional icon, label and sub-label.
import { stat } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import sharp from 'sharp'

export const KEY_SIZE = 118
const ICON_SIZE = 56
const ICON_TOP = 8
const DEFAULT_BASE = dirname(fileURLToPath(import.meta.url))

const iconCache = new Map() // "path:mtime" -> PNG buffer of the resized icon
const iconFailures = new Set() // paths already logged as broken, so each logs once

function escapeXml(s) {
	return String(s).replace(/[<>&'"]/g, (c) => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', "'": '&apos;', '"': '&quot;' })[c])
}

function labelSvg(key, pressed, withIcon) {
	const label = escapeXml(key.label ?? '')
	const sub = escapeXml(key.sub ?? '')
	let fontSize, labelY, subSize, subY
	if (withIcon) {
		// Icon takes the top 64px; label and sub-label share what's left.
		fontSize = label.length <= 6 ? 18 : 14
		labelY = sub ? 84 : 94
		subSize = 13
		subY = 104
	} else {
		fontSize = label.length <= 4 ? 34 : label.length <= 6 ? 26 : 20
		labelY = sub ? 62 : 70
		subSize = 16
		subY = 92
	}
	return `<svg xmlns="http://www.w3.org/2000/svg" width="${KEY_SIZE}" height="${KEY_SIZE}">
		<rect width="100%" height="100%" fill="${pressed ? '#ffffff' : (key.bg ?? '#000000')}"/>
		<text x="50%" y="${labelY}" font-family="Ubuntu, sans-serif" font-weight="bold" font-size="${fontSize}"
			fill="${pressed ? '#000000' : (key.fg ?? '#ffffff')}" text-anchor="middle">${label}</text>
		${sub ? `<text x="50%" y="${subY}" font-family="Ubuntu, sans-serif" font-size="${subSize}" fill="${pressed ? '#333333' : '#cccccc'}" text-anchor="middle">${sub}</text>` : ''}
	</svg>`
}

// The icon resized to fit ICON_SIZE, or null if it is missing or unreadable.
async function loadIcon(icon, baseDir) {
	const path = resolve(baseDir, icon)
	try {
		const { mtimeMs } = await stat(path)
		const cacheKey = `${path}:${mtimeMs}`
		if (!iconCache.has(cacheKey)) {
			const input = path.toLowerCase().endsWith('.svg') ? sharp(path, { density: 300 }) : sharp(path)
			const png = await input
				.resize(ICON_SIZE, ICON_SIZE, { fit: 'contain', background: { r: 0, g: 0, b: 0, alpha: 0 } })
				.png()
				.toBuffer()
			for (const k of iconCache.keys()) if (k.startsWith(`${path}:`)) iconCache.delete(k)
			iconCache.set(cacheKey, png)
		}
		iconFailures.delete(path)
		return iconCache.get(cacheKey)
	} catch (e) {
		if (!iconFailures.has(path)) {
			iconFailures.add(path)
			console.error(`icon ${icon} unusable, drawing without it: ${e.message}`)
		}
		return null
	}
}

// Returns a PNG buffer, or for format 'raw' a 118x118 RGB buffer ready for fillKeyBuffer.
export async function renderKey(key, { pressed = false, format = 'raw', baseDir = DEFAULT_BASE } = {}) {
	const icon = key.icon ? await loadIcon(key.icon, baseDir) : null
	let img = sharp(Buffer.from(labelSvg(key, pressed, !!icon)))
	if (icon) img = img.composite([{ input: icon, top: ICON_TOP, left: Math.round((KEY_SIZE - ICON_SIZE) / 2) }])
	const png = await img.png().toBuffer()
	if (format === 'png') return png
	if (format !== 'raw') throw new Error(`unknown render format: ${format}`)
	const { data, info } = await sharp(png).flatten().removeAlpha().raw().toBuffer({ resolveWithObject: true })
	if (info.channels !== 3) throw new Error(`expected 3 channels, got ${info.channels}`)
	return data
}
