// Runs key actions: shell commands, app launches,
// key chords via ydotool, prompt pastes via wl-copy, and daemon builtins.
import { spawn, execFile } from 'node:child_process'

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
let queue = Promise.resolve()

// The key's action, falling back to the legacy fields: action > builtin > run.
export function resolveAction(key) {
	if (!key) return null
	if (key.action) return key.action
	if (key.builtin) return { type: 'builtin', name: key.builtin }
	if (key.run) return { type: 'command', command: key.run }
	return null
}

// Waits for a program to exit; rejects on a spawn error, non-zero exit or timeout.
// A child still running after `timeout` ms is SIGKILLed so it can't jam the action queue.
export function run(cmd, args, { input, detached = false, timeout = 5000 } = {}) {
	return new Promise((resolve, reject) => {
		const child = spawn(cmd, args, { stdio: [input == null ? 'ignore' : 'pipe', 'ignore', 'ignore'], detached })
		const timer = setTimeout(() => {
			child.kill('SIGKILL')
			reject(new Error(`${cmd} timed out after ${timeout} ms`))
		}, timeout)
		child.on('error', (e) => {
			clearTimeout(timer)
			reject(new Error(`${cmd}: ${e.message}`))
		})
		child.on('exit', (code, signal) => {
			clearTimeout(timer)
			code === 0 ? resolve() : reject(new Error(`${cmd} exited with ${signal ?? code}`))
		})
		if (input != null) {
			child.stdin.on('error', () => {}) // EPIPE if the child dies first; 'exit' reports it
			child.stdin.end(input)
		}
	})
}

// Fire and forget: resolves once the shell has started.
function runCommand(command) {
	return new Promise((resolve, reject) => {
		const child = spawn('sh', ['-c', command], { detached: true, stdio: 'ignore' })
		child.on('error', (e) => reject(new Error(`command failed: ${command}: ${e.message}`)))
		child.on('spawn', () => {
			child.unref()
			resolve()
		})
	})
}

// One ydotool call: press each code in order, release in reverse.
function pressKeys(codes) {
	if (!Array.isArray(codes) || codes.length === 0 || !codes.every(Number.isInteger)) throw new Error(`bad key codes: ${JSON.stringify(codes)}`)
	const args = ['key', ...codes.map((c) => `${c}:1`), ...[...codes].reverse().map((c) => `${c}:0`)]
	return run('ydotool', args)
}

function readClipboard() {
	return new Promise((resolve) =>
		execFile('wl-paste', ['--no-newline', '--type', 'text'], { timeout: 2000, maxBuffer: 16 << 20 }, (err, out) => resolve(err ? null : out)),
	)
}

function writeClipboard(text) {
	return run('wl-copy', ['--type', 'text/plain'], { input: text })
}

async function prompt({ text, submit = false, pasteCodes = [29, 42, 47], restoreClipboard = false }) {
	if (typeof text !== 'string') throw new Error('prompt action needs a text string')
	const saved = restoreClipboard ? await readClipboard() : null
	await writeClipboard(text)
	await sleep(50)
	await pressKeys(pasteCodes)
	if (submit) {
		await sleep(250)
		await pressKeys([28])
	}
	if (restoreClipboard && saved != null) {
		await sleep(1000)
		await writeClipboard(saved)
	}
}

// Runs one action and throws on failure. `onBuiltin(name)` handles daemon builtins.
export async function runAction(action, { onBuiltin } = {}) {
	switch (action?.type) {
		case 'command':
			return runCommand(action.command)
		case 'app':
			return run('gtk-launch', [action.desktopId], { detached: true })
		case 'keys':
			return pressKeys(action.codes)
		case 'prompt':
			return prompt(action)
		case 'builtin':
			if (!onBuiltin) return console.error(`builtin ${action.name} ignored: no handler`)
			return onBuiltin(action.name)
		default:
			throw new Error(`unknown action type: ${action?.type}`)
	}
}

// Queues the key's action behind any still running, so double presses don't interleave.
// Errors are logged, never thrown.
export function executeAction(key, opts = {}) {
	const action = resolveAction(key)
	if (!action) return queue
	queue = queue.then(() => runAction(action, opts)).catch((e) => console.error(`action failed: ${e?.message ?? e}`))
	return queue
}
