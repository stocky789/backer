using System;

namespace Backer.Desktop.Services;

/// <summary>A continuous five-second hold; cancellation discards all elapsed time.</summary>
public sealed class ConfirmationHold(TimeProvider? timeProvider = null)
{
    private readonly TimeProvider _time = timeProvider ?? TimeProvider.System;
    private long? _started;

    public void Start() => _started ??= _time.GetTimestamp();

    public void Cancel() => _started = null;

    public double SecondsRemaining => _started is { } started
        ? Math.Max(0, 5 - _time.GetElapsedTime(started).TotalSeconds)
        : 5;

    public bool Complete => _started is not null && SecondsRemaining == 0;
}
