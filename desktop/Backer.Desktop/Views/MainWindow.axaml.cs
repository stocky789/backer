using Avalonia.Controls;
using Avalonia.Input;
using Avalonia.Markup.Xaml;
using Backer.Desktop.ViewModels;

namespace Backer.Desktop.Views;

public partial class MainWindow : Window
{
    public MainWindow()
    {
        AvaloniaXamlLoader.Load(this);
        KeyDown += OnKeyDown;
    }

    private void OnKeyDown(object? sender, KeyEventArgs e)
    {
        if (DataContext is not MainWindowViewModel viewModel)
        {
            return;
        }
        switch (e.Key)
        {
            case Key.Escape:
                viewModel.GoHome();
                e.Handled = true;
                break;
            case Key.Enter:
                viewModel.InvokePrimary();
                e.Handled = true;
                break;
        }
    }
}
