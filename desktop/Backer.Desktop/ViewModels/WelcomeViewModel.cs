using Backer.Desktop.Services;
using CommunityToolkit.Mvvm.Input;

namespace Backer.Desktop.ViewModels;

public sealed partial class WelcomeViewModel : ViewModelBase
{
    private readonly MainWindowViewModel? _shell;

    public WelcomeViewModel(MainWindowViewModel? shell = null) => _shell = shell;

    public override string Title => "Welcome";

    public string Heading => CanSetUp ? "Back up this computer" : "Local backups unavailable";

    public string Body => CanSetUp
        ? "Keep a copy of your files on storage you own, or join a server."
        : "Join a server in Settings to get started.";

    /// <summary>No proven storage on this platform means no local setup path.</summary>
    public bool CanSetUp => Cells.SupportedRepositoryTypes().Count > 0;

    public override IRelayCommand? PrimaryCommand => CanSetUp ? SetUpLocalBackupsCommand : null;

    [RelayCommand]
    private void SetUpLocalBackups() => _shell?.Navigate("repository");
}
