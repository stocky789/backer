using System;
using System.Linq;
using Avalonia.Controls;
using Avalonia.Data.Converters;
using Avalonia.Input;
using Avalonia.Markup.Xaml;
using Backer.Desktop.ViewModels;

namespace Backer.Desktop.Views;

public partial class MainWindow : Window
{
    // Bound the page even inside the scroll viewer so logs and tables keep their own scrolling.
    public static FuncMultiValueConverter<double, double> PageWidth { get; } =
        new(values => Math.Max(820, values.First() / values.Last()));

    public static FuncMultiValueConverter<double, double> PageHeight { get; } =
        new(values => Math.Max(480, values.First() / values.Last()));

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
