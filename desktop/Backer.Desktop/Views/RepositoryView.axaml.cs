using Avalonia.Controls;
using Avalonia.Markup.Xaml;
using Backer.Desktop.ViewModels;

namespace Backer.Desktop.Views;

public partial class RepositoryView : UserControl
{
    public RepositoryView() => AvaloniaXamlLoader.Load(this);

    /// <summary>A share was picked: list its root folders. The VM reset the location first.</summary>
    private void OnShareSelected(object? sender, SelectionChangedEventArgs e)
    {
        if (DataContext is RepositoryViewModel vm && vm.SelectedShare is not null)
        {
            vm.BrowseCommand.Execute(null);
        }
    }
}
