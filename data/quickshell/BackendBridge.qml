pragma ComponentBehavior: Bound

import QtQuick
import Quickshell.Io

Item {
  id: bridge
  visible: false

  property int nextRequestId: 1
  property bool ready: false
  property var queuedRequests: []
  property var latestRequests: ({})
  property var latestMethods: ({})

  signal response(string method, int requestId, var result)
  signal failure(string method, int requestId, string message)
  signal eventReceived(string name, var data)

  function request(method, args) {
    var requestId = nextRequestId++
    var line = JSON.stringify({
      id: requestId,
      method: method,
      args: args || {}
    }) + "\n"
    if (ready) bridgeProcess.write(line)
    else {
      queuedRequests.push(line)
      if (!bridgeProcess.running) bridgeProcess.running = true
    }
    return requestId
  }

  // Coalesce repeated reads and suppress superseded results, even when a
  // query changes A → B → A while the first A is still in flight.
  function requestLatest(method, args) {
    latestMethods[method] = true
    const active = latestRequests[method]
    if (active) {
      active.queued = args
      active.cancelled = false
    } else {
      latestRequests[method] = {id: request(method, args), cancelled: false}
    }
  }

  function cancelLatest(method) {
    const active = latestRequests[method]
    if (active) {
      active.queued = undefined
      active.cancelled = true
    }
  }

  function handleLine(line) {
    try {
      var payload = JSON.parse(line)
      const active = latestRequests[payload.method]
      if (latestMethods[payload.method] && (!active || active.id !== payload.id)) return
      if (active && active.id === payload.id) {
        delete latestRequests[payload.method]
        if (active.queued !== undefined) {
          requestLatest(payload.method, active.queued)
          return
        }
        if (active.cancelled) return
      }
      if (typeof payload.event === "string") {
        eventReceived(payload.event, payload.data)
      } else if (payload.ok === true) {
        response(payload.method || "", payload.id || 0, payload.result)
      } else {
        if (!payload.method) latestRequests = ({})
        failure(payload.method || "", payload.id || 0,
                payload.error || "BlueFerry request failed")
      }
    } catch (error) {
      latestRequests = ({})
      failure("", 0, "BlueFerry bridge returned invalid data")
    }
  }

  Process {
    id: bridgeProcess
    running: true
    command: ["/usr/bin/blueferry-quickshell-bridge"]
    stdinEnabled: true
    stdout: SplitParser {
      onRead: function(line) { bridge.handleLine(line) }
    }
    onStarted: {
      bridge.ready = true
      for (var index = 0; index < bridge.queuedRequests.length; ++index)
        bridgeProcess.write(bridge.queuedRequests[index])
      bridge.queuedRequests = []
    }
    // qmllint disable signal-handler-parameters
    onExited: function(code) {
      bridge.ready = false
      bridge.queuedRequests = []
      bridge.latestRequests = ({})
      bridge.failure("", 0, code === 0
        ? "BlueFerry bridge stopped"
        : "BlueFerry bridge is unavailable")
    }
  }
}
