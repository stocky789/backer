using System.Threading.Tasks;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using Backer.Desktop.Services;

namespace Backer.Desktop.Views;

/// <summary>
/// The only modal in the app. Declining is the default: the dialog returns false unless the
/// user presses the confirm button, and with a typed confirmation that button stays disabled
/// until the exact word is typed.
/// </summary>
public static class ConfirmDialog
{
    public static async Task<bool> ShowAsync(Window owner, ConfirmRequest request)
    {
        var confirmed = false;

        var confirm = new Button { Content = request.ConfirmLabel, IsEnabled = request.TypedConfirmation is null };
        // The one place Danger is allowed on a button: the destructive confirmation itself.
        confirm.Classes.Add("danger");
        var cancel = new Button { Content = "Cancel", IsCancel = true, IsDefault = true };
        var buttons = new StackPanel
        {
            Orientation = Orientation.Horizontal,
            Spacing = 8,
            HorizontalAlignment = HorizontalAlignment.Right,
        };
        buttons.Children.Add(cancel);
        buttons.Children.Add(confirm);

        var panel = new StackPanel { Spacing = 16, Margin = new Avalonia.Thickness(24) };
        panel.Children.Add(new TextBlock { Text = request.Body, TextWrapping = TextWrapping.Wrap });
        if (request.TypedConfirmation is { } word)
        {
            panel.Children.Add(new TextBlock { Text = $"Type {word} to continue" });
            var entry = new TextBox();
            entry.TextChanged += (_, _) => confirm.IsEnabled = entry.Text == word;
            panel.Children.Add(entry);
        }
        panel.Children.Add(buttons);

        var dialog = new Window
        {
            Title = request.Title,
            Content = panel,
            Width = 460,
            SizeToContent = SizeToContent.Height,
            CanResize = false,
            ShowInTaskbar = false,
            WindowStartupLocation = WindowStartupLocation.CenterOwner,
        };
        confirm.Click += (_, _) =>
        {
            confirmed = true;
            dialog.Close();
        };
        cancel.Click += (_, _) => dialog.Close();

        await dialog.ShowDialog(owner);
        return confirmed;
    }
}
