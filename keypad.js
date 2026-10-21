// Daemon for the Logitech MX Creative Keypad: draws labelled keys from config.json,
// runs each key's shell command on press, and flips between pages with the page buttons.
import { spawn, exec } from 'node:child_process'
import { readFileSync, watch } from 'node:fs'
import { fileURLToPath } from 'node:url'
import sharp from 'sharp'
import { listMXCreativeConsoleDevices, openMxCreativeConsole } from '@logitech-mx-creative-console/node'

const CONFIG_PATH = fileURLToPath(new URL('config.json', import.meta.url))
const KEY_SIZE = 118
const KEY_COUNT = 9
const RECONNECT_MS = 2000
const STATE_POLL_MS = 1500

let config = loadConfig()
let device = null
let page = 0
let brightness = config.brightness ?? 80
const keyState = new Map() // "page:key" -> boolean, for keys with a `state` command

function loadConfig() {
	const parsed = JSON.parse(readFileSync(CONFIG_PATH, 'utf8'))
	if (!Array.isArray(parsed.pages) || parsed.pages.length === 0) throw new Error('config.json needs a non-empty "pages" array')
	return parsed
}

function escapeXml(s) {
	return String(s).replace(/[<>&'"]/g, (c) => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', "'": '&apos;', '"': '&quot;' })[c])
}

async function renderKey(key, pressed) {
	const label = escapeXml(key.label ?? '')
	const sub = escapeXml(key.sub ?? '')
	const fontSize = label.length <= 4 ? 34 : label.length <= 6 ? 26 : 20
	const labelY = sub ? 62 : 70
	const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${KEY_SIZE}" height="${KEY_SIZE}">
		<rect width="100%" height="100%" fill="${pressed ? '#ffffff' : (key.bg ?? '#000000')}"/>
		<text x="50%" y="${labelY}" font-family="Ubuntu, sans-serif" font-weight="bold" font-size="${fontSize}"
			fill="${pressed ? '#000000' : (key.fg ?? '#ffffff')}" text-anchor="middle">${label}</text>
		${sub ? `<text x="50%" y="92" font-family="Ubuntu, sans-serif" font-size="16" fill="${pressed ? '#333333' : '#cccccc'}" text-anchor="middle">${sub}</text>` : ''}
	</svg>`
	return sharp(Buffer.from(svg)).flatten().raw().toBuffer()
}

// The key as it should look now: its `on` overrides apply while its state command reports true.
function effectiveKey(pageIndex, keyIndex) {
	const key = config.pages[pageIndex]?.keys?.[keyIndex]
	if (!key) return null
	return keyState.get(`${pageIndex}:${keyIndex}`) && key.on ? { ...key, ...key.on } : key
}

async function drawKey(keyIndex, pressed = false) {
	if (!device) return
	const key = effectiveKey(page, keyIndex)
	if (!key) return device.clearKey(keyIndex)
	await device.fillKeyBuffer(keyIndex, await renderKey(key, pressed))
}

async function drawPage() {
	for (let i = 0; i < KEY_COUNT; i++) await drawKey(i)
}

function runCommand(cmd) {
	const child = spawn('sh', ['-c', cmd], { detached: true, stdio: 'ignore' })
	child.on('error', (e) => console.error(`command failed: ${cmd}:`, e.message))
	child.unref()
}

function checkState(cmd) {
	return new Promise((resolve) => exec(cmd, { timeout: 2000 }, (err) => resolve(!err)))
}

// Re-runs the state commands for the visible page and redraws keys whose state changed.
async function pollState() {
	const keys = config.pages[page]?.keys ?? []
	for (let i = 0; i < keys.length && i < KEY_COUNT; i++) {
		if (!keys[i]?.state) continue
		const id = `${page}:${i}`
		const now = await checkState(keys[i].state)
		if (keyState.get(id) !== now) {
			keyState.set(id, now)
			await drawKey(i).catch(logError)
		}
	}
}

async function setBrightness(value) {
	brightness = Math.max(10, Math.min(100, value))
	await device?.setBrightness(brightness)
}

async function onDown(control) {
	if (control.type !== 'button') return
	if (control.id === 'page-l' || control.id === 'page-r') {
		const n = config.pages.length
		page = (page + (control.id === 'page-r' ? 1 : n - 1)) % n
		console.log(`page ${page}: ${config.pages[page].name ?? ''}`)
		await pollState()
		return drawPage()
	}
	const key = config.pages[page]?.keys?.[control.index]
	if (!key) return
	console.log(`key ${control.index} (${key.label}) pressed`)
	await drawKey(control.index, true)
	if (key.builtin === 'brightness-up') await setBrightness(brightness + 10)
	else if (key.builtin === 'brightness-down') await setBrightness(brightness - 10)
	else if (key.run) runCommand(key.run)
}

async function onUp(control) {
	if (control.type !== 'button' || control.id?.startsWith('page-')) return
	await drawKey(control.index)
	// Give toggles a moment to take effect, then show the new state.
	if (config.pages[page]?.keys?.[control.index]?.state) setTimeout(() => pollState().catch(logError), 300)
}

function logError(e) {
	console.error(e?.message ?? e)
}

async function disconnect() {
	const old = device
	device = null
	await old?.close().catch(() => {})
	console.log('keypad disconnected')
}

async function connect() {
	const info = (await listMXCreativeConsoleDevices()).find((d) => d.modelInfo.id === 'mx-creative-keypad')
	if (!info) return
	const dev = await openMxCreativeConsole(info.path)
	dev.on('error', (e) => {
		logError(e)
		if (device === dev) disconnect()
	})
	dev.on('down', (c) => onDown(c).catch(logError))
	dev.on('up', (c) => onUp(c).catch(logError))
	device = dev
	console.log(`keypad connected at ${info.path}`)
	await dev.setBrightness(brightness)
	await pollState()
	await drawPage()
}

setInterval(() => {
	if (!device) connect().catch((e) => device || logError(e))
}, RECONNECT_MS)
setInterval(() => {
	if (device) pollState().catch(logError)
}, STATE_POLL_MS)

// Editors often replace the file instead of writing it, so watch the directory.
let reloadTimer
watch(fileURLToPath(new URL('.', import.meta.url)), (_event, name) => {
	if (name !== 'config.json') return
	clearTimeout(reloadTimer)
	reloadTimer = setTimeout(async () => {
		try {
			config = loadConfig()
			if (page >= config.pages.length) page = 0
			keyState.clear()
			console.log('config reloaded')
			await pollState()
			await drawPage()
		} catch (e) {
			console.error('config reload failed, keeping the old one:', e.message)
		}
	}, 200)
})

async function shutdown() {
	await device?.resetToLogo().catch(() => {})
	await device?.close().catch(() => {})
	process.exit(0)
}
process.on('SIGINT', shutdown)
process.on('SIGTERM', shutdown)

connect().catch(logError)
