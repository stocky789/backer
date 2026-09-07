using System;
using System.Threading.Tasks;
using Avalonia.Controls;
using Avalonia.Layout;
using Avalonia.Media;
using Avalonia.Threading;
using Backer.Desktop.Services;
using Backer.Desktop.ViewModels;

namespace Backer.Desktop.Views;

/// <summary>
/// The only modal in the app. Declining is the default: the dialog returns false unless the
/// user confirms. Repository deletion requires a continuous hold; restore replacement uses
/// an exact typed confirmation.
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
        if (request.HoldToConfirm)
        {
            panel.Children.Add(new TextBlock
            {
                Text = "Hold Delete for 5 seconds. Release to cancel. You can also focus the button and hold Space.",
                TextWrapping = TextWrapping.Wrap,
            });
        }
        if (request.TypedConfirmation is { } word)
        {
            panel.Children.Add(new TextBlock { Text = $"Type {word} to continue" });
            var entry = new TextBox();
            entry.TextChanged += (_, _) => confirm.IsEnabled = entry.Text == word;
            panel.Children.Add(entry);
        }
        panel.Children.Add(buttons);

        var scale = (owner.DataContext as MainWindowViewModel)?.Settings.UiScale ?? 1;
        var dialog = new Window
        {
            Title = request.Title,
            Content = new LayoutTransformControl
            {
                LayoutTransform = new ScaleTransform(scale, scale),
                Child = panel,
            },
            Width = 460 * scale,
            SizeToContent = SizeToContent.Height,
            CanResize = false,
            ShowInTaskbar = false,
            WindowStartupLocation = WindowStartupLocation.CenterOwner,
        };
        confirm.Click += (_, _) =>
        {
            if (request.HoldToConfirm)
            {
                return;
            }
            confirmed = true;
            dialog.Close();
        };
        cancel.Click += (_, _) => dialog.Close();

        if (request.HoldToConfirm)
        {
            var hold = new ConfirmationHold();
            var timer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(50) };
            var label = $"Hold {request.ConfirmLabel} (5s)";
            confirm.Content = label;
            void CancelHold()
            {
                timer.Stop();
                hold.Cancel();
                confirm.Content = label;
            }
            confirm.PropertyChanged += (_, change) =>
            {
                if (change.Property != Button.IsPressedProperty)
                {
                    return;
                }
                if (confirm.IsPressed && dialog.IsActive)
                {
                    hold.Start();
                    timer.Start();
                }
                else
                {
                    CancelHold();
                }
            };
            timer.Tick += (_, _) =>
            {
                if (!confirm.IsPressed || !dialog.IsActive)
                {
                    CancelHold();
                    return;
                }
                confirm.Content = $"Hold {request.ConfirmLabel} ({Math.Ceiling(hold.SecondsRemaining)}s)";
                if (hold.Complete)
                {
                    CancelHold();
                    confirmed = true;
                    dialog.Close();
                }
            };
            confirm.PointerExited += (_, _) => CancelHold();
            confirm.PointerCaptureLost += (_, _) => CancelHold();
            confirm.LostFocus += (_, _) => CancelHold();
            dialog.Deactivated += (_, _) => CancelHold();
            dialog.Closed += (_, _) => CancelHold();
        }

        await dialog.ShowDialog(owner);
        return confirmed;
    }
}
