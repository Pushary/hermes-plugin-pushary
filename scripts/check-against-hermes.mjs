import { spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { homedir } from 'node:os'
import { delimiter, dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import ts from 'typescript'

const PACKAGE_DIRECTORY = join(dirname(fileURLToPath(import.meta.url)), '..')
const SOURCE_FILE = resolve(PACKAGE_DIRECTORY, 'desktop-src', 'plugin.ts')
const HERMES_AGENT_DIRECTORY = process.env.HERMES_AGENT_DIR || join(homedir(), '.hermes', 'hermes-agent')
const DESKTOP_DIRECTORY = join(HERMES_AGENT_DIRECTORY, 'apps', 'desktop')
const SDK_ENTRY = join(DESKTOP_DIRECTORY, 'src', 'sdk', 'index.ts')
const HERMES_PYTHON = join(HERMES_AGENT_DIRECTORY, 'venv', 'bin', 'python3')
export const TESTS_THAT_NEED_HERMES = ['test_hermes_contract.py', 'test_desktop_backend.py']

export const compilerOptionsForInstalledHermes = (desktopDirectory) => {
  const configFile = join(desktopDirectory, 'tsconfig.json')
  const parsed = ts.parseJsonConfigFileContent(ts.readConfigFile(configFile, ts.sys.readFile).config, ts.sys, desktopDirectory)
  const react = join(HERMES_AGENT_DIRECTORY, 'node_modules', '@types', 'react')
  return {
    ...parsed.options,
    noEmit: true,
    verbatimModuleSyntax: true,
    baseUrl: desktopDirectory,
    paths: { ...parsed.options.paths, react: [react], 'react/*': [`${react}/*`] },
    typeRoots: [join(HERMES_AGENT_DIRECTORY, 'node_modules', '@types')],
    types: ['node'],
  }
}

export const pluginDiagnostics = (desktopDirectory) => {
  const program = ts.createProgram([SOURCE_FILE], compilerOptionsForInstalledHermes(desktopDirectory))
  return ts.getPreEmitDiagnostics(program).filter((diagnostic) => diagnostic.file && resolve(diagnostic.file.fileName) === SOURCE_FILE)
}

const fail = (message) => {
  console.error(message)
  process.exit(1)
}

const typecheckAgainstTheInstalledSdk = () => {
  const diagnostics = pluginDiagnostics(DESKTOP_DIRECTORY)
  if (diagnostics.length === 0) return
  console.error(ts.formatDiagnostics(diagnostics, {
    getCanonicalFileName: (name) => name,
    getCurrentDirectory: () => PACKAGE_DIRECTORY,
    getNewLine: () => '\n',
  }))
  fail('desktop-src/plugin.ts does not typecheck against the Hermes Desktop SDK installed on this machine.')
}

const runTestsInsideHermes = (pattern) => {
  const result = spawnSync(HERMES_PYTHON, ['-m', 'unittest', 'discover', '-s', 'tests', '-p', pattern], {
    cwd: PACKAGE_DIRECTORY,
    env: { ...process.env, PYTHONPATH: [PACKAGE_DIRECTORY, HERMES_AGENT_DIRECTORY].join(delimiter), PUSHARY_REQUIRE_HERMES: '1' },
    stdio: 'inherit',
  })
  if (result.status !== 0) fail(`${pattern} failed inside the Hermes Agent at ${HERMES_AGENT_DIRECTORY}.`)
}

const main = () => {
  if (!existsSync(SDK_ENTRY) || !existsSync(HERMES_PYTHON)) {
    fail(
      `No Hermes Agent at ${HERMES_AGENT_DIRECTORY}, so the plugin cannot be checked against the host it runs in.\n`
      + 'Install Hermes Agent, point HERMES_AGENT_DIR at a checkout with its venv, or skip deliberately with PUSHARY_GATE_SKIP=hermes.',
    )
  }
  typecheckAgainstTheInstalledSdk()
  for (const pattern of TESTS_THAT_NEED_HERMES) runTestsInsideHermes(pattern)
  console.log(`hermes plugin: checked against the Hermes Agent in ${HERMES_AGENT_DIRECTORY}`)
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) main()
