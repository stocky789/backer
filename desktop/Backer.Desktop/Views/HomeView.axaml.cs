using Avalonia.Controls;
using Avalonia.Input;
using Avalonia.Markup.Xaml;
using Backer.Desktop.ViewModels;

namespace Backer.Desktop.Views;

public partial class HomeView : UserControl
{
    public HomeView() => AvaloniaXamlLoader.Load(this);

    private void OnRowDoubleTapped(object? sender, TappedEventArgs e)
    {
        if (DataContext is HomeViewModel viewModel && viewModel.SelectedJob is not null)
        {
            viewModel.BackUpNowCommand.Execute(null);
        }
    }
}
