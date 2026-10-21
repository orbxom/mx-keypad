// Daemon for the Logitech MX Creative Keypad: draws labelled keys from config.json,
// runs each key's action on press, and flips between pages with the page buttons.
import { exec } from 'node:child_process'
import { readFileSync, watch } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { listMXCreativeConsoleDevices, openMxCreativeConsole } from '@logitech-mx-creative-console/node'
import { renderKey } from './render.js'
import { executeAction } from './actions.js'

const CONFIG_DIR = fileURLToPath(new URL('.', import.meta.url))
const CONFIG_PATH = fileURLToPath(new URL('config.json', import.meta.url))
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
	await device.fillKeyBuffer(keyIndex, await renderKey(key, { pressed, format: 'raw', baseDir: CONFIG_DIR }), { format: 'rgb' })
}

async function drawPage() {
	for (let i = 0; i < KEY_COUNT; i++) await drawKey(i)
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
	const key = effectiveKey(page, control.index)
	if (!key) return
	console.log(`key ${control.index} (${key.label}) pressed`)
	await drawKey(control.index, true)
	executeAction(key, { onBuiltin })
}

function onBuiltin(name) {
	if (name === 'brightness-up') return setBrightness(brightness + 10)
	if (name === 'brightness-down') return setBrightness(brightness - 10)
	throw new Error(`unknown builtin: ${name}`)
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
