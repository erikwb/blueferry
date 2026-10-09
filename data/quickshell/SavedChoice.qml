import QtQuick

// An on/off setting saved through the daemon. The checkbox shows `value`,
// which keeps the user's choice while the save runs and is not undone by a
// status that was requested before the save finished.
QtObject {
  property bool value: false
  property bool busy: false
  property bool statusStale: false

  function request(choice) {
    value = choice
    busy = true
  }

  // `statusInFlight`: a status request is still unanswered and may report
  // the old choice.
  function saved(choice, statusInFlight) {
    busy = false
    value = choice
    statusStale = statusInFlight
  }

  function failed(reported) {
    busy = false
    value = reported
  }

  function reported(choice) {
    if (statusStale) statusStale = false
    else if (!busy) value = choice
  }
}
