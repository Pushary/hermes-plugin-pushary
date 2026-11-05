(function registerPusharyDashboardPlugin() {
  var sdk = window.__HERMES_PLUGIN_SDK__
  var plugins = window.__HERMES_PLUGINS__
  if (!sdk || !plugins) return
  var h = sdk.React.createElement

  function PusharyDashboardNote() {
    return h('p', { style: { padding: '16px', color: 'var(--color-muted-foreground)' } },
      'Pushary approvals show in the Hermes Desktop status bar and on your phone.')
  }

  plugins.register('pushary', PusharyDashboardNote)
})()
