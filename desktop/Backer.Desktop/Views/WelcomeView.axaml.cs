using Avalonia.Controls;
using Avalonia.Markup.Xaml;

namespace Backer.Desktop.Views;

public partial class WelcomeView : UserControl
{
    public WelcomeView() => AvaloniaXamlLoader.Load(this);
}
