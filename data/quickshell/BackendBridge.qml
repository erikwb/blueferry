pragma ComponentBehavior: Bound

import QtQuick
import Quickshell.Io

Item {
  id: bridge
  visible: false

  property int nextRequestId: 1
  property bool ready: false
  property var queuedRequests: []

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

  function handleLine(line) {
    try {
      var payload = JSON.parse(line)
      if (typeof payload.event === "string") {
        eventReceived(payload.event, payload.data)
      } else if (payload.ok === true) {
        response(payload.method || "", payload.id || 0, payload.result)
      } else {
        failure(payload.method || "", payload.id || 0,
                payload.error || "BlueFerry request failed")
      }
    } catch (error) {
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
      bridge.failure("", 0, code === 0
        ? "BlueFerry bridge stopped"
        : "BlueFerry bridge is unavailable")
    }
  }
}
