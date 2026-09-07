using System;
using Backer.Desktop.Services;
using Xunit;

namespace Backer.Desktop.Tests;

public sealed class ConfirmationHoldTests
{
    private sealed class Clock : TimeProvider
    {
        public long Milliseconds;
        public override long TimestampFrequency => 1000;
        public override long GetTimestamp() => Milliseconds;
    }

    [Fact]
    public void OnlyAnUninterruptedFiveSecondHoldCompletes()
    {
        var clock = new Clock();
        var hold = new ConfirmationHold(clock);
        Assert.False(hold.Complete);
        hold.Start();
        clock.Milliseconds = 4999;
        Assert.False(hold.Complete);
        hold.Cancel();
        clock.Milliseconds = 10000;
        Assert.False(hold.Complete);
        Assert.Equal(5, hold.SecondsRemaining);
        hold.Start();
        clock.Milliseconds = 14999;
        hold.Start(); // Key repeat must not reset an ongoing hold.
        Assert.False(hold.Complete);
        clock.Milliseconds = 15000;
        Assert.True(hold.Complete);
        hold.Cancel();
        Assert.False(hold.Complete);
    }
}
