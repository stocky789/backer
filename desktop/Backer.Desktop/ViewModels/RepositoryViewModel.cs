using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.Linq;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using Backer.Desktop.Services;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;

namespace Backer.Desktop.ViewModels;

public sealed class ShareRow
{
    public string? Name { get; set; }

    public string? Comment { get; set; }

    public override string ToString() => Name ?? "";
}

/// <summary>One directory row from `repo browse` (snake_case: name, is_dir, is_repository).</summary>
public sealed class BrowseEntry
{
    public string? Name { get; set; }

    public bool IsDir { get; set; }

    /// <summary>The folder already holds a repository, so it cannot be --init'd — only attached.</summary>
    public bool IsRepository { get; set; }
}

/// <summary>The `repo browse --json` object: the listed path and its subdirectories.</summary>
public sealed class BrowseResult
{
    public string? Path { get; set; }

    public List<BrowseEntry>? Entries { get; set; }
}

/// <summary>
/// The repository wizard: one view, staged steps. Every mutation is a `backer` CLI call and
/// the CLI owns all rollback — nothing half-created is remembered here.
/// </summary>
public sealed partial class RepositoryViewModel : ViewModelBase
{
    /// <summary>Same split as backer.serverless.repositories.passphrase_words.</summary>
    private static readonly Regex WordSplit = new(@"[\s-]+", RegexOptions.Compiled);

    private readonly AppServices _services;
    private readonly MainWindowViewModel? _shell;

    public RepositoryViewModel(AppServices services, MainWindowViewModel? shell = null)
    {
        _services = services;
        _shell = shell;
        RepositoryType = SupportedTypes.FirstOrDefault() ?? "local";
    }

    public RepositoryViewModel()
        : this(new AppServices())
    {
    }

    public override string Title => "Add repository";

    public IReadOnlyList<string> SupportedTypes { get; } = Cells.SupportedRepositoryTypes();

    // ---- step machine -------------------------------------------------------

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(IsTypeStep), nameof(IsDetailStep), nameof(IsPassphraseStep))]
    [NotifyPropertyChangedFor(nameof(CanGoBack), nameof(PrimaryLabel), nameof(StepLabel))]
    private string _step = "type";

    /// <summary>Quiet step indicator; the steps really are a sequence, so it earns its place.</summary>
    public string StepLabel => Step switch
    {
        "type" => "STEP 1 OF 3 · WHERE TO KEEP BACKUPS",
        "detail" => "STEP 2 OF 3 · STORAGE DETAILS",
        "passphrase" => "STEP 3 OF 3 · RECOVERY COPY",
        _ => "",
    };

    public bool IsTypeStep => Step == "type";

    public bool IsDetailStep => Step == "detail";

    public bool IsPassphraseStep => Step == "passphrase";

    public bool CanGoBack => Step is "detail" or "passphrase" && !Created;

    public string PrimaryLabel => Step switch
    {
        "type" => "Continue",
        "detail" when IsFiles => "Create repository",
        "detail" => "Continue",
        "passphrase" => Created ? "Done" : "Create repository",
        _ => "Done",
    };

    public override IRelayCommand PrimaryCommand => ContinueCommand;

    [ObservableProperty]
    private string _statusText = "";

    [ObservableProperty]
    private bool _busy;

    // ---- storage choice -----------------------------------------------------

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(IsLocal), nameof(IsSmb), nameof(IsS3))]
    private string _repositoryType = "local";

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(IsKopia), nameof(IsFiles), nameof(UseKopia), nameof(UseFiles), nameof(PrimaryLabel), nameof(CanContinue))]
    private string _format = "kopia";

    public bool IsKopia => Format == "kopia";

    public bool IsFiles => Format == "files";

    public bool UseKopia
    {
        get => IsKopia;
        set
        {
            if (value)
            {
                Format = "kopia";
            }
        }
    }

    public bool UseFiles
    {
        get => IsFiles;
        set
        {
            if (value)
            {
                Format = "files";
            }
        }
    }

    public bool IsLocal => RepositoryType == "local";

    public bool IsSmb => RepositoryType == "smb";

    public bool IsS3 => RepositoryType == "s3";

    [ObservableProperty]
    private string _name = "";

    /// <summary>Attach an existing repository instead of creating one.</summary>
    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(CanGenerate), nameof(ShowAttachHint))]
    private bool _attach;

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(BrowseLocation), nameof(CanGoUp), nameof(IsAtShareRoot))]
    private string _path = "";

    [ObservableProperty]
    private string _host = "";

    [ObservableProperty]
    private string _username = "";

    [ObservableProperty]
    private string _password = "";

    [ObservableProperty]
    private string _domain = "";

    public ObservableCollection<ShareRow> Shares { get; } = new();

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(BrowseLocation), nameof(HasSelectedShare))]
    private ShareRow? _selectedShare;

    /// <summary>Subfolders of the current browsed location; a repository row is flagged.</summary>
    public ObservableCollection<BrowseEntry> Folders { get; } = new();

    [ObservableProperty]
    private bool _browseBusy;

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(ShowAttachHint))]
    private bool _currentIsRepository;

    [ObservableProperty]
    private string _newFolderName = "";

    public bool HasSelectedShare => SelectedShare is not null;

    /// <summary>Not at share root — an "Up" step is possible.</summary>
    public bool CanGoUp => Path.Trim().Length > 0;

    public bool IsAtShareRoot => Path.Trim().Length == 0;

    /// <summary>Where the browser is now; the share name alone stands for its root.</summary>
    public string BrowseLocation => SelectedShare is null
        ? ""
        : Path.Trim().Length == 0 ? SelectedShare.Name ?? "" : $"{SelectedShare.Name}/{Path.Trim()}";

    /// <summary>The current folder is already a repository: guide toward attach, since --init can't reuse it.</summary>
    public bool ShowAttachHint => CurrentIsRepository && !Attach;

    /// <summary>Selecting a share drops the browser back to that share's root.</summary>
    partial void OnSelectedShareChanged(ShareRow? value)
    {
        Path = "";
        CurrentIsRepository = false;
        NewFolderName = "";
        Folders.Clear();
    }

    [ObservableProperty]
    private string _bucket = "";

    [ObservableProperty]
    private string _prefix = "";

    [ObservableProperty]
    private string _endpoint = "";

    [ObservableProperty]
    private string _region = "";

    [ObservableProperty]
    private string _accessKeyId = "";

    [ObservableProperty]
    private string _secretKey = "";

    // ---- passphrase ---------------------------------------------------------

    /// <summary>
    /// Generating means the CLI mints the passphrase during `repo add`, so the reveal and its
    /// confirmation come straight after that call. A user-supplied passphrase is confirmed first
    /// and nothing is created until it is.
    /// </summary>
    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(CanContinue), nameof(ShowSupplied))]
    private bool _useGenerated = true;

    public bool CanGenerate => !Attach;

    /// <summary>The other half of the radio pair; a plain negation binding is not writable.</summary>
    public bool UseSupplied
    {
        get => !UseGenerated;
        set
        {
            if (value)
            {
                UseGenerated = false;
            }
        }
    }

    partial void OnUseGeneratedChanged(bool value) => OnPropertyChanged(nameof(UseSupplied));

    public bool ShowSupplied => !UseGenerated || Attach;

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(CanContinue), nameof(ShowReveal), nameof(RevealedPassphrase))]
    private string _suppliedPassphrase = "";

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(CanContinue), nameof(ShowReveal), nameof(RevealedPassphrase))]
    private string _suppliedConfirmation = "";

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(CanContinue), nameof(ShowReveal), nameof(RevealedPassphrase))]
    [NotifyPropertyChangedFor(nameof(PrimaryLabel), nameof(CanGoBack), nameof(CanSaveRecoveryRecord))]
    private bool _created;

    [ObservableProperty]
    private string _generatedPassphrase = "";

    public string? RepositoryId { get; private set; }

    /// <summary>The passphrase currently on screen; empty when there is nothing to reveal.</summary>
    public string RevealedPassphrase => Created
        ? GeneratedPassphrase
        : SuppliedPassphrase == SuppliedConfirmation ? SuppliedPassphrase : "";

    /// <summary>Attaching reuses an existing passphrase, so there is nothing to reveal or save.</summary>
    public bool NeedsConfirmation => IsKopia && !Attach;

    public bool ShowReveal => NeedsConfirmation && RevealedPassphrase.Length > 0;

    public bool CanContinue => Step switch
    {
        "detail" when IsFiles => true,
        "passphrase" when Created => true,
        // Generating hands the passphrase over only after `repo add`; supplying one needs a match.
        "passphrase" => UseGenerated && !Attach
            ? true
            : SuppliedPassphrase.Length > 0 && SuppliedPassphrase == SuppliedConfirmation,
        _ => true,
    };

    public static IReadOnlyList<string> PassphraseWords(string value) =>
        WordSplit.Split(value.Trim()).Where(word => word.Length > 0).ToList();

    public override void OnShown()
    {
        if (Created)
        {
            Reset();
        }
    }

    private void Reset()
    {
        Step = "type";
        Created = false;
        RepositoryId = null;
        GeneratedPassphrase = "";
        SuppliedPassphrase = SuppliedConfirmation = "";
        StatusText = "";
    }

    // ---- navigation ---------------------------------------------------------

    [RelayCommand]
    private void Back()
    {
        Step = Step switch
        {
            "passphrase" => "detail",
            "detail" => "type",
            _ => Step,
        };
    }

    [RelayCommand]
    public async Task ContinueAsync()
    {
        if (Busy)
        {
            return;
        }
        switch (Step)
        {
            case "type":
                Step = "detail";
                if (IsSmb && Shares.Count == 0)
                {
                    StatusText = "Enter the file server and sign-in, then list the shares.";
                }
                break;
            case "detail":
                if (DetailProblem() is { } problem)
                {
                    StatusText = problem;
                    return;
                }
                AutoName();
                if (IsFiles)
                {
                    await CreateAsync();
                }
                else
                {
                    Step = "passphrase";
                }
                break;
            case "passphrase" when !Created:
                if (!CanContinue)
                {
                    StatusText = "Enter the passphrase in both fields to continue.";
                    return;
                }
                await CreateAsync();
                break;
            case "passphrase":
                // The generated passphrase has been shown (and can be saved); the wizard is done.
                Finish();
                break;
        }
    }

    private string? DetailProblem() => RepositoryType switch
    {
        "s3" when IsFiles => "Unencrypted files repositories support local folders and SMB shares, not S3.",
        "local" when Path.Trim().Length == 0 => "Choose a folder for the repository.",
        "smb" when Host.Trim().Length == 0 || Username.Trim().Length == 0 || Password.Length == 0 =>
            "File server credentials are required.",
        "smb" when SelectedShare is null || Path.Trim().Length == 0 => "Choose a share and a folder.",
        "s3" when new[] { Bucket, Endpoint, Region, AccessKeyId, SecretKey }.Any(value => value.Trim().Length == 0) =>
            "All S3 fields are required.",
        _ => null,
    };

    private void AutoName()
    {
        if (Name.Trim().Length > 0)
        {
            return;
        }
        var candidate = RepositoryType switch
        {
            "s3" => Prefix.Trim().Length > 0 ? Prefix : Bucket,
            "smb" => Path.Trim().Length > 0 ? Path : SelectedShare?.Name ?? "",
            _ => Path,
        };
        var parts = candidate.Split(new[] { '/', '\\' }, StringSplitOptions.RemoveEmptyEntries);
        Name = parts.Length > 0 ? parts[^1] : "Repository";
    }

    [RelayCommand]
    public async Task ChooseFolderAsync()
    {
        var folder = await _services.PickFolder();
        if (folder is not null)
        {
            Path = folder;
        }
    }

    [RelayCommand]
    public async Task CopyPassphraseAsync()
    {
        if (RevealedPassphrase.Length == 0)
        {
            return;
        }
        await _services.CopyText(RevealedPassphrase);
        StatusText = "Copied. The clipboard is not durable storage; save the passphrase somewhere safe.";
    }

    /// <summary>The recovery record is a plain-text file; the user acknowledges that first.</summary>
    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(CanSaveRecoveryRecord))]
    private bool _plainTextAck;

    public static string PlainTextAcknowledgement => RecoveryRecord.Acknowledgement;

    public bool CanSaveRecoveryRecord => PlainTextAck && RevealedPassphrase.Length > 0;

    /// <summary>
    /// `backer repo passphrase NAME --passphrase-out FILE` — the CLI reads the keystore and
    /// writes the file (mode 600); the GUI only picks the folder.
    /// </summary>
    [RelayCommand]
    public async Task SaveRecoveryRecordAsync()
    {
        if (!PlainTextAck)
        {
            StatusText = "Tick the acknowledgement first: the recovery record is plain text.";
            return;
        }
        if (!Created)
        {
            StatusText = "The repository is created first; then its recovery record can be saved.";
            return;
        }
        var folder = await _services.PickFolder();
        if (folder is null)
        {
            StatusText = "Nothing was written.";
            return;
        }
        var destination = RecoveryRecord.Destination(folder, Name);
        Busy = true;
        var result = await _services.Cli.RunAsync(RecoveryRecord.Arguments(Name.Trim(), destination));
        Busy = false;
        StatusText = result.Ok ? result.Stdout.Trim() : result.FailureText;
        _services.Status.Set(StatusText, error: !result.Ok);
    }

    /// <summary>`backer repo discover` — one named host, never a network scan.</summary>
    [RelayCommand]
    public async Task ListSharesAsync()
    {
        if (Host.Trim().Length == 0 || Username.Trim().Length == 0 || Password.Length == 0)
        {
            StatusText = "File server credentials are required.";
            return;
        }
        Busy = true;
        StatusText = "Loading shares…";
        var discoverArgs = new List<string>
        {
            "repo", "discover", "--host", Host.Trim(), "--username", Username.Trim(), "--password-stdin", "--json",
        };
        if (Domain.Trim().Length > 0)
        {
            discoverArgs.AddRange(new[] { "--domain", Domain.Trim() });
        }
        var result = await _services.Cli.RunAsync(discoverArgs.ToArray(), stdin: Password);
        Busy = false;
        if (!result.Ok)
        {
            StatusText = result.FailureText;
            _services.Status.Set(result.FailureText, error: true);
            return;
        }
        Shares.Clear();
        foreach (var share in result.Json<List<ShareRow>>() ?? new List<ShareRow>())
        {
            Shares.Add(share);
        }
        SelectedShare = Shares.FirstOrDefault();
        StatusText = Shares.Count > 0 ? "Choose a share." : "No shares were listed for that sign-in.";
    }

    /// <summary>argv for `repo browse` at the current location. The password rides on stdin, never here.</summary>
    public IReadOnlyList<string> BuildBrowseArguments()
    {
        var arguments = new List<string>
        {
            "repo", "browse", "--host", Host.Trim(), "--share", SelectedShare?.Name ?? "",
        };
        if (Path.Trim().Length > 0)
        {
            arguments.AddRange(new[] { "--path", Path.Trim() });
        }
        arguments.AddRange(new[] { "--username", Username.Trim() });
        if (Domain.Trim().Length > 0)
        {
            arguments.AddRange(new[] { "--domain", Domain.Trim() });
        }
        arguments.AddRange(new[] { "--password-stdin", "--json" });
        return arguments;
    }

    /// <summary>`backer repo browse` — list the subfolders of the current location. Same auth as discover.</summary>
    [RelayCommand]
    public async Task BrowseAsync()
    {
        if (SelectedShare is null)
        {
            return;
        }
        BrowseBusy = true;
        StatusText = "Loading folders…";
        var result = await _services.Cli.RunAsync(BuildBrowseArguments(), stdin: Password);
        BrowseBusy = false;
        if (!result.Ok)
        {
            StatusText = result.FailureText;
            _services.Status.Set(result.FailureText, error: true);
            return;
        }
        Folders.Clear();
        var listing = result.Json<BrowseResult>();
        foreach (var entry in (listing?.Entries ?? new List<BrowseEntry>()).Where(e => e.IsDir))
        {
            Folders.Add(entry);
        }
        StatusText = Folders.Count > 0 ? "Choose a folder, or name a new one." : "This folder is empty.";
    }

    /// <summary>Descend into a folder: it becomes the browsed location, then its contents are listed.</summary>
    [RelayCommand]
    public async Task DescendAsync(BrowseEntry? entry)
    {
        if (entry?.Name is not { Length: > 0 } name)
        {
            return;
        }
        Path = Path.Trim().Length == 0 ? name : $"{Path.Trim()}/{name}";
        CurrentIsRepository = entry.IsRepository;
        NewFolderName = "";
        await BrowseAsync();
    }

    /// <summary>Pop one segment off the browsed location and re-list.</summary>
    [RelayCommand]
    public async Task UpAsync()
    {
        var trimmed = Path.Trim();
        if (trimmed.Length == 0)
        {
            return;
        }
        var slash = trimmed.LastIndexOf('/');
        Path = slash < 0 ? "" : trimmed[..slash];
        CurrentIsRepository = false;
        NewFolderName = "";
        await BrowseAsync();
    }

    /// <summary>
    /// Name a subfolder under the current location and select it. No mkdir is spawned — `repo add
    /// --init` creates missing folders, so the folder appears only when the repository is created.
    /// </summary>
    [RelayCommand]
    public void AddNewFolder()
    {
        var name = NewFolderName.Trim().Trim('/');
        if (name.Length == 0)
        {
            return;
        }
        Path = Path.Trim().Length == 0 ? name : $"{Path.Trim()}/{name}";
        CurrentIsRepository = false;
        NewFolderName = "";
        Folders.Clear();
        StatusText = "This folder is created when the repository is added.";
    }

    // ---- CLI calls ----------------------------------------------------------

    /// <summary>argv for `repo add`. Secrets never appear here.</summary>
    public IReadOnlyList<string> BuildRepoAddArguments()
    {
        var arguments = new List<string>
        {
            "repo", "add", Name.Trim(), Attach ? "--attach" : "--init", "--headless", "--type", RepositoryType,
        };
        if (IsFiles)
        {
            arguments.AddRange(new[] { "--format", "files" });
        }
        switch (RepositoryType)
        {
            case "local":
                arguments.AddRange(new[] { "--path", Path.Trim() });
                break;
            case "smb":
                arguments.AddRange(new[] { "--host", Host.Trim(), "--share", SelectedShare?.Name ?? "", "--path", Path.Trim() });
                arguments.AddRange(new[] { "--username", Username.Trim() });
                if (Domain.Trim().Length > 0)
                {
                    arguments.AddRange(new[] { "--domain", Domain.Trim() });
                }
                break;
            case "s3":
                arguments.AddRange(new[] { "--bucket", Bucket.Trim(), "--endpoint", Endpoint.Trim(), "--region", Region.Trim() });
                if (Prefix.Trim().Length > 0)
                {
                    arguments.AddRange(new[] { "--prefix", Prefix.Trim() });
                }
                arguments.AddRange(new[] { "--access-key-id", AccessKeyId.Trim() });
                break;
        }
        if (IsFiles)
        {
            return arguments;
        }
        if (UseGenerated && !Attach)
        {
            arguments.AddRange(new[] { "--generate-passphrase", "--print-passphrase" });
        }
        else
        {
            arguments.Add("--passphrase-stdin");
        }
        return arguments;
    }

    /// <summary>
    /// The CLI refuses two stdin secrets in one call, so the storage secret rides in the child
    /// environment (never argv, never a file) while stdin carries the passphrase.
    /// </summary>
    private Dictionary<string, string> StorageEnvironment() => RepositoryType switch
    {
        "smb" => new Dictionary<string, string> { ["BACKER_SMB_PASSWORD"] = Password },
        "s3" => new Dictionary<string, string> { ["BACKER_S3_SECRET_KEY"] = SecretKey },
        _ => new Dictionary<string, string>(),
    };

    public async Task CreateAsync()
    {
        Busy = true;
        StatusText = "Creating the repository…";
        var generating = IsKopia && UseGenerated && !Attach;
        var result = await _services.Cli.RunAsync(
            BuildRepoAddArguments(),
            stdin: IsFiles || generating ? null : SuppliedPassphrase,
            environment: StorageEnvironment());
        Busy = false;
        if (!result.Ok)
        {
            // The CLI's own wording, verbatim; the CLI already rolled back its own partial state.
            // With --print-passphrase the generated passphrase is on stdout, so this one path
            // must never fall back to stdout for its failure text.
            var failure = generating ? result.StderrOnlyFailureText : result.FailureText;
            StatusText = failure;
            _services.Status.Set(failure, error: true);
            return;
        }

        RepositoryId = ParseRepositoryId(result.Stdout);
        if (generating)
        {
            GeneratedPassphrase = ParsePrintedPassphrase(result.Stdout);
        }
        SuppliedPassphrase = SuppliedConfirmation = "";
        Created = true;
        _services.Status.Set(result.Stdout.Trim().Split('\n').LastOrDefault() ?? "Repository saved");
        // A supplied passphrase was confirmed before this call, so there is nothing left to
        // reveal — the wizard is done. Only a generated one pauses here to be shown and saved.
        if (!generating)
        {
            Finish();
            return;
        }
        StatusText = "Save this passphrase now. It is the only key to these backups.";
    }

    /// <summary>`Repository 'x' saved (keyring, id 9f8e7d6c5b4a)`.</summary>
    public static string? ParseRepositoryId(string stdout)
    {
        var match = Regex.Match(stdout, @"\bid ([0-9a-f]{6,})\)");
        return match.Success ? match.Groups[1].Value : null;
    }

    /// <summary>--print-passphrase writes the phrase on its own line before the saved line.</summary>
    public static string ParsePrintedPassphrase(string stdout)
    {
        foreach (var line in stdout.Split('\n'))
        {
            var trimmed = line.Trim();
            if (trimmed.Length > 0 && !trimmed.StartsWith("Repository", StringComparison.Ordinal)
                && !trimmed.StartsWith("Warning", StringComparison.Ordinal))
            {
                return trimmed;
            }
        }
        return "";
    }

    /// <summary>
    /// The repository is saved; the wizard's job is done. A backup job is created separately
    /// from Home, so hand back a status that points the user there.
    /// </summary>
    private void Finish()
    {
        Step = "done";
        _services.Status.Set($"Repository '{Name.Trim()}' added. Create a backup job to start backing up.");
        _shell?.GoHome();
    }

    [RelayCommand]
    private void Cancel() => _shell?.GoHome();
}
