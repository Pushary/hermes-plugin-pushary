import { readFileSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import ts from 'typescript'

const PACKAGE_DIRECTORY = join(dirname(fileURLToPath(import.meta.url)), '..')
export const SOURCE_FILE = join(PACKAGE_DIRECTORY, 'desktop-src', 'plugin.ts')
export const OUTPUT_FILE = join(PACKAGE_DIRECTORY, 'pushary_plugin', 'desktop', 'plugin.js')
export const ALLOWED_SPECIFIERS = new Set(['@hermes/plugin-sdk', 'react', 'react/jsx-runtime'])

const BANNER = '// Generated from desktop-src/plugin.ts by scripts/build-desktop.mjs. Edit the source, then run it.\n'

const COMPILER_OPTIONS = {
  target: ts.ScriptTarget.ES2022,
  module: ts.ModuleKind.ESNext,
  verbatimModuleSyntax: true,
  removeComments: true,
  newLine: ts.NewLineKind.LineFeed,
}

export const buildDesktopPlugin = (source) => {
  const { outputText, diagnostics } = ts.transpileModule(source, {
    compilerOptions: COMPILER_OPTIONS,
    fileName: 'plugin.ts',
    reportDiagnostics: true,
  })
  if (diagnostics && diagnostics.length > 0) {
    throw new Error(ts.formatDiagnostics(diagnostics, {
      getCanonicalFileName: (name) => name,
      getCurrentDirectory: () => PACKAGE_DIRECTORY,
      getNewLine: () => '\n',
    }))
  }
  return `${BANNER}${outputText}`
}

export const importedSpecifiers = (javascript) => {
  const file = ts.createSourceFile('plugin.js', javascript, ts.ScriptTarget.ES2022, false, ts.ScriptKind.JS)
  const specifiers = []
  const visit = (node) => {
    if ((ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) && node.moduleSpecifier && ts.isStringLiteral(node.moduleSpecifier)) {
      specifiers.push(node.moduleSpecifier.text)
    }
    if (ts.isCallExpression(node) && node.expression.kind === ts.SyntaxKind.ImportKeyword) specifiers.push('<dynamic import>')
    ts.forEachChild(node, visit)
  }
  visit(file)
  return specifiers
}

export const unexpectedSpecifiers = (javascript) =>
  importedSpecifiers(javascript).filter((specifier) => !ALLOWED_SPECIFIERS.has(specifier))

const main = () => {
  const check = process.argv.includes('--check')
  const built = buildDesktopPlugin(readFileSync(SOURCE_FILE, 'utf-8'))
  const unexpected = unexpectedSpecifiers(built)
  if (unexpected.length > 0) {
    console.error(`Hermes Desktop loads only ${[...ALLOWED_SPECIFIERS].join(', ')}; plugin.ts imports ${unexpected.join(', ')}`)
    process.exit(1)
  }
  if (!check) {
    writeFileSync(OUTPUT_FILE, built)
    console.log(`built ${OUTPUT_FILE}`)
    return
  }
  if (readFileSync(OUTPUT_FILE, 'utf-8') !== built) {
    console.error('pushary_plugin/desktop/plugin.js is not the build of desktop-src/plugin.ts.')
    console.error('Run: node packages/hermes-plugin/scripts/build-desktop.mjs')
    process.exit(1)
  }
  console.log('hermes desktop half: built from its source')
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) main()
