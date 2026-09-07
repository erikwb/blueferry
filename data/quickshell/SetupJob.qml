import QtQuick
import Quickshell.Io

// One helper per request. A replaced request cannot overwrite a new job's ID.
Item {
  id: job
  required property int requestId
  required property string kind
  required property var command
  property bool interactive: false
  // Pairing includes authorization, discovery, and multiple Bluetooth waits.
  property int timeoutMs: interactive ? 600000 : kind === "devices" ? 90000 : 60000
  property bool exited: false
  property bool stdoutDone: false
  property bool stderrDone: false
  property bool delivered: false
  property int exitCode: -1
  property int consumed: 0
  signal lineReceived(int requestId, string kind, string line)
  signal finished(int requestId, string kind, int code, string output, string diagnostic)

  function start() { process.running = true; deadline.start() }
  function write(text) { process.write(text) }
  function cancel() {
    delivered = true
    process.signal(15)
    deadline.interval = 2000
    deadline.restart()
  }
  function complete() {
    if (!exited || !stdoutDone || !stderrDone || delivered) return
    delivered = true
    deadline.stop()
    finished(requestId, kind, exitCode, output.text, errors.text.trim())
  }

  Process {
    id: process
    command: job.command
    stdinEnabled: job.interactive
    stdout: StdioCollector {
      id: output
      waitForEnd: false
      onTextChanged: {
        if (!job.interactive) return
        let end = text.indexOf("\n", job.consumed)
        while (end >= 0) {
          const line = text.slice(job.consumed, end)
          job.consumed = end + 1
          job.lineReceived(job.requestId, job.kind, line)
          end = text.indexOf("\n", job.consumed)
        }
      }
      onStreamFinished: { job.stdoutDone = true; job.complete() }
    }
    stderr: StdioCollector {
      id: errors
      onStreamFinished: { job.stderrDone = true; job.complete() }
    }
    // qmllint disable signal-handler-parameters
    onExited: function(code) {
      job.exitCode = code
      job.exited = true
      if (job.delivered) job.destroy()
      else job.complete()
    }
  }

  Timer {
    id: deadline
    interval: job.timeoutMs
    onTriggered: {
      process.signal(9)
      if (!job.delivered) {
        job.delivered = true
        job.finished(job.requestId, job.kind, -1, "", "Setup helper timed out or could not start.")
      } else job.destroy()
    }
  }
}
