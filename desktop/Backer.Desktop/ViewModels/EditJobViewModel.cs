using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Threading.Tasks;
using Backer.Desktop.Services;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;

namespace Backer.Desktop.ViewModels;

/// <summary>
/// Home's "Edit": schedule, retention, excludes and enabled for one existing job. Saving is
/// `backer job set NAME ...` with only the flags the user actually changed — the CLI validates
/// everything and its wording is what the user reads.
/// </summary>
public sealed partial class EditJobViewModel : ViewModelBase
{
    private readonly AppServices _services;
    private readonly MainWindowViewModel? _shell;

    private JobConfig _original = new();

    public EditJobViewModel(AppServices services, MainWindowViewModel? shell = null)
    {
        _services = services;
        _shell = shell;
    }

    public EditJobViewModel()
        : this(new AppServices())
    {
    }

    public override string Title => JobName.Length > 0 ? $"Edit '{JobName}'" : "Edit backup job";

    public override IRelayCommand PrimaryCommand => SaveCommand;

    [ObservableProperty]
    private string _jobName = "";

    [ObservableProperty]
    private string _sourcePath = "";

    [ObservableProperty]
    private string _cron = "";

    [ObservableProperty]
    private bool _noSchedule;

    [ObservableProperty]
    private string _excludes = "";

    [ObservableProperty]
    private string _keepLast = "";

    [ObservableProperty]
    private string _keepDaily = "";

    [ObservableProperty]
    private string _keepWeekly = "";

    [ObservableProperty]
    private string _keepMonthly = "";

    [ObservableProperty]
    private string _keepYearly = "";

    [ObservableProperty]
    private bool _enabled = true;

    [ObservableProperty]
    private string _statusText = "";

    [ObservableProperty]
    private bool _busy;

    /// <summary>Fills the fields from config.yaml. The GUI never writes that file.</summary>
    public void Load(string jobName)
    {
        JobName = jobName;
        StatusText = "";
        try
        {
            var config = _services.Config.Load();
            _original = config.Jobs.TryGetValue(jobName, out var job) ? job : new JobConfig();
        }
        catch (Exception error)
        {
            _original = new JobConfig();
            StatusText = error.Message;
            _services.Status.Set(error.Message, error: true);
        }
        SourcePath = _original.Source?.Path ?? "";
        Cron = _original.Schedule?.Cron ?? "";
        NoSchedule = Cron.Length == 0;
        Excludes = string.Join("\n", _original.Source?.Excludes ?? new List<string>());
        KeepLast = Number(_original.Retention?.KeepLast);
        KeepDaily = Number(_original.Retention?.KeepDaily);
        KeepWeekly = Number(_original.Retention?.KeepWeekly);
        KeepMonthly = Number(_original.Retention?.KeepMonthly);
        KeepYearly = Number(_original.Retention?.KeepYearly);
        Enabled = _original.Enabled;
    }

    private static string Number(int? value) =>
        value is null ? "" : value.Value.ToString(CultureInfo.InvariantCulture);

    /// <summary>argv for `job set`, carrying only what changed. Empty means nothing changed.</summary>
    public IReadOnlyList<string> BuildArguments()
    {
        var arguments = new List<string>();

        var originalCron = _original.Schedule?.Cron ?? "";
        if (NoSchedule)
        {
            if (originalCron.Length > 0)
            {
                arguments.Add("--no-schedule");
            }
        }
        else if (Cron.Trim().Length > 0 && Cron.Trim() != originalCron)
        {
            arguments.AddRange(new[] { "--schedule", Cron.Trim() });
        }

        foreach (var (flag, value, before) in new[]
                 {
                     ("--keep-last", KeepLast, _original.Retention?.KeepLast),
                     ("--keep-daily", KeepDaily, _original.Retention?.KeepDaily),
                     ("--keep-weekly", KeepWeekly, _original.Retention?.KeepWeekly),
                     ("--keep-monthly", KeepMonthly, _original.Retention?.KeepMonthly),
                     ("--keep-yearly", KeepYearly, _original.Retention?.KeepYearly),
                 })
        {
            if (value.Trim().Length > 0 && value.Trim() != Number(before))
            {
                arguments.AddRange(new[] { flag, value.Trim() });
            }
        }

        var excludes = JobFields.ExcludeLines(Excludes);
        if (!excludes.SequenceEqual(_original.Source?.Excludes ?? new List<string>()))
        {
            if (excludes.Count == 0)
            {
                arguments.Add("--clear-excludes");
            }
            else
            {
                foreach (var pattern in excludes)
                {
                    arguments.AddRange(new[] { "--exclude", pattern });
                }
            }
        }

        if (Enabled != _original.Enabled)
        {
            arguments.Add(Enabled ? "--enable" : "--disable");
        }

        return arguments.Count == 0
            ? Array.Empty<string>()
            : new[] { "job", "set", JobName }.Concat(arguments).ToList();
    }

    [RelayCommand]
    public async Task SaveAsync()
    {
        var arguments = BuildArguments();
        if (arguments.Count == 0)
        {
            StatusText = "Nothing was changed.";
            return;
        }
        Busy = true;
        try
        {
            var result = await _services.Cli.RunAsync(arguments);
            if (!result.Ok)
            {
                // The CLI validates the cron string and the keep-* values; its wording, verbatim.
                StatusText = result.FailureText;
                _services.Status.Set(result.FailureText, error: true);
                return;
            }
            _services.Status.Set($"'{JobName}' updated");
            _shell?.Home.Reload();
            _shell?.GoHome();
        }
        finally
        {
            Busy = false;
        }
    }

    [RelayCommand]
    private void Cancel() => _shell?.GoHome();
}
