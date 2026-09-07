pragma ComponentBehavior: Bound

import QtQuick

Item {
  id: transport
  property var jobs: ({})
  signal lineReceived(int requestId, string kind, string line)
  signal finished(int requestId, string kind, int code, string output, string diagnostic)

  function execute(id, kind, command, interactive) {
    const job = jobComponent.createObject(transport, {
      requestId: id, kind: kind, command: command, interactive: interactive
    }) as SetupJob
    if (!job) {
      finished(id, kind, -1, "", "Could not create the setup helper.")
      return
    }
    jobs[id] = job
    job.start()
  }
  function cancel(id) {
    const job = jobs[id]
    delete jobs[id]
    if (job) job.cancel()
  }
  function write(id, text) {
    if (jobs[id]) jobs[id].write(text)
  }

  Component {
    id: jobComponent
    SetupJob {
      onLineReceived: (id, kind, line) => transport.lineReceived(id, kind, line)
      onFinished: (id, kind, code, output, diagnostic) => {
        const job = transport.jobs[id]
        delete transport.jobs[id]
        transport.finished(id, kind, code, output, diagnostic)
        if (job) job.destroy()
      }
    }
  }
}
